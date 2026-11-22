import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

import credit_limit_calls
import credit_limit_common
import db
import main
import multi_active_terminators_patch
import terminator_balance_patch


class AudioIvrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_path = patch.object(db, "DB_PATH", Path(self.temp.name) / "billing.db")
        db_path.start()
        self.addCleanup(db_path.stop)
        db.init_db()
        credit_limit_common.ensure_schema(db)
        terminator_balance_patch.ensure_schema(db)
        self.app = FastAPI()
        credit_limit_calls.install_call_routes(self.app, main, db)
        self.app.dependency_overrides[main.require_api_key] = lambda: True
        self.http = TestClient(self.app)
        self.addCleanup(self.http.close)
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO clients (id, name, sip_ip, balance_cents) VALUES (1, 'OneClick', '192.0.2.10', 1000000)"
            )
            conn.execute(
                "INSERT INTO terminators "
                "(id, name, destination_name, prefix, gateway_name, cost_rate_cents, billing_cycle, route_mode, audio_asset, audio_max_seconds) "
                "VALUES (10, 'Audio IVR', 'Czech Republic', '420', '', 0, '1/1', 'audio', 'greeting.wav', 45)"
            )
            conn.execute(
                "INSERT INTO client_rates "
                "(id, client_id, terminator_id, prefix, destination_name, sell_rate_cents, billing_cycle, voice_node) "
                "VALUES (20, 1, 10, '420', 'Czech Republic', 1000, '6/6', 'primary')"
            )

    def test_audio_route_reserves_without_supplier_gateway(self):
        response = self.http.post(
            "/api/reserve",
            json={
                "sip_ip": "192.0.2.10",
                "destination": "420123456789",
                "call_uuid": "audio-call-1",
                "clid": "420700000000",
                "voice_node": "primary",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["route_mode"], "audio")
        self.assertEqual(result["audio_asset"], "greeting.wav")
        self.assertEqual(result["gateway_name"], "audio:greeting.wav")
        self.assertEqual(result["route_ip"], "")
        self.assertEqual(result["cost_rate_cents"], 0)
        self.assertEqual(result["sell_billing_cycle"], "6/6")
        self.assertLessEqual(result["max_seconds"], 45)

    def test_audio_finalize_charges_client_but_not_supplier(self):
        reserve = self.http.post(
            "/api/reserve",
            json={"sip_ip": "192.0.2.10", "destination": "420123456789", "call_uuid": "audio-call-2", "voice_node": "primary"},
        ).json()
        response = self.http.post(
            "/api/finalize",
            json={
                "client_id": 1,
                "call_uuid": "audio-call-2",
                "sip_ip": "192.0.2.10",
                "destination": "420123456789",
                "dial_destination": reserve["dial_destination"],
                "provider_number": reserve["provider_number"],
                "billsec": 7,
                "sell_rate_cents": reserve["sell_rate_cents"],
                "cost_rate_cents": reserve["cost_rate_cents"],
                "sell_billing_cycle": reserve["sell_billing_cycle"],
                "cost_billing_cycle": reserve["cost_billing_cycle"],
                "gateway_name": "audio:greeting.wav",
                "terminator_id": 10,
                "terminator_name": "Audio IVR",
                "terminator_destination_name": "Czech Republic",
                "terminator_prefix": "420",
                "result": "AUDIO_IVR",
                "voice_node": "primary",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertGreater(result["charged_cents"], 0)
        self.assertEqual(result["margin_cents"], result["charged_cents"])
        self.assertEqual(result["supplier_charge"]["status"], "no_cost")
        with db.get_conn() as conn:
            cdr = conn.execute("SELECT * FROM cdr WHERE call_uuid = 'audio-call-2'").fetchone()
            self.assertEqual(cdr["gateway_name"], "audio:greeting.wav")
            self.assertEqual(cdr["result"], "AUDIO_IVR")

    def test_legacy_sip_terminators_default_to_sip_mode(self):
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO terminators (name, ips, destination_name, prefix, gateway_name, cost_rate_cents) "
                "VALUES ('Legacy', '203.0.113.9', 'Germany', '49', '', 100)"
            )
            row = conn.execute("SELECT * FROM terminators WHERE name = 'Legacy'").fetchone()
            self.assertEqual(row["route_mode"], "sip")
            self.assertEqual(row["audio_asset"], "")

    def test_audio_asset_name_cannot_escape_audio_directory(self):
        self.assertEqual(db.normalize_audio_asset("greeting.wav"), "greeting.wav")
        self.assertEqual(db.normalize_audio_asset("../secret.wav"), "")
        self.assertEqual(db.normalize_audio_asset("https://example.com/a.wav"), "")
        self.assertEqual(db.normalize_audio_asset("greeting.mp3"), "")

    def test_admin_creation_forces_audio_route_to_zero_supplier_cost(self):
        result = main.create_terminator(
            main.TerminatorIn(
                name="Local announcement",
                gateway_group_id=None,
                destination_name="Germany",
                prefix="49",
                cost_rate_cents=9999,
                route_mode="audio",
                audio_asset="notice.wav",
                audio_max_seconds=60,
            )
        )
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM terminators WHERE id = ?", (result["id"],)).fetchone()
            self.assertEqual(row["route_mode"], "audio")
            self.assertEqual(row["cost_rate_cents"], 0)
            self.assertIsNone(row["gateway_group_id"])
            self.assertEqual(row["gateway_name"], "")

    def test_admin_creation_rejects_invalid_audio_asset(self):
        with self.assertRaises(HTTPException) as raised:
            main.create_terminator(
                main.TerminatorIn(
                    name="Unsafe",
                    destination_name="Germany",
                    prefix="49",
                    cost_rate_cents=0,
                    route_mode="audio",
                    audio_asset="../secret.wav",
                )
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_multi_active_admin_endpoint_accepts_audio_without_gateway(self):
        app = FastAPI()
        multi_active_terminators_patch.install(app, main, db)
        app.dependency_overrides[main.require_admin] = lambda: "test"
        http = TestClient(app)
        self.addCleanup(http.close)

        response = http.post(
            "/api/terminators",
            json={
                "name": "IVR Germany",
                "destination_name": "Germany",
                "prefix": "49",
                "cost_rate_cents": 9999,
                "billing_cycle": "1/1",
                "route_mode": "audio",
                "audio_asset": "lexico_ivr_20261122.wav",
                "audio_max_seconds": 300,
                "active": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM terminators WHERE id = ?", (response.json()["id"],)).fetchone()
            self.assertEqual(row["route_mode"], "audio")
            self.assertEqual(row["audio_asset"], "lexico_ivr_20261122.wav")
            self.assertEqual(row["cost_rate_cents"], 0)
            self.assertEqual(row["gateway_name"], "")
            self.assertIsNone(row["gateway_group_id"])


if __name__ == "__main__":
    unittest.main()
