import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import credit_limit_clients
import credit_limit_common
import db
import main
import multi_sip_credentials_patch as multi_sip


class MultiSipCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_path = patch.object(db, "DB_PATH", Path(self.temp.name) / "billing.db")
        db_path.start()
        self.addCleanup(db_path.stop)
        credit_limit_common.ensure_schema(db)
        self.app = FastAPI()
        credit_limit_clients.install_client_routes(self.app, main, db)
        multi_sip.install(self.app, main, db)
        self.app.dependency_overrides[main.require_api_key] = lambda: True
        self.app.dependency_overrides[main.require_admin] = lambda: "test"
        self.app.dependency_overrides[main.require_current_dashboard] = lambda: True
        self.http = TestClient(self.app)
        self.addCleanup(self.http.close)

    def create_sip_client(self):
        response = self.http.post(
            "/api/clients",
            json={"name": "Agents", "connection_mode": "sip", "sip_ip": ""},
            headers={"X-Money-Scale": str(db.MONEY_SCALE)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def create_credential(self, client_id, label="Agent 1", login=""):
        response = self.http.post(
            f"/api/clients/{client_id}/sip-credentials",
            json={"label": label, "sip_login": login},
            headers={"X-Money-Scale": str(db.MONEY_SCALE)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_sip_only_client_and_multiple_credentials(self):
        client_id = self.create_sip_client()
        first = self.create_credential(client_id)
        second = self.create_credential(client_id, "Agent 2")
        self.assertNotEqual(first["sip_login"], second["sip_login"])
        self.assertEqual(len(first["sip_password"]), 20)
        self.assertEqual(first["server"], multi_sip.SIP_SERVER)
        rows = self.http.get(f"/api/clients/{client_id}/sip-credentials").json()
        self.assertEqual([row["label"] for row in rows], ["Agent 1", "Agent 2"])
        with db.get_conn() as conn:
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            self.assertEqual(client["connection_mode"], "sip")
            self.assertTrue(client["sip_ip"].startswith("sip-only:"))

    def test_only_dedicated_profile_can_resolve_login(self):
        client_id = self.create_sip_client()
        credential = self.create_credential(client_id, login="office.agent")
        with db.get_conn() as conn:
            valid = multi_sip.resolve_client(
                conn,
                SimpleNamespace(profile=multi_sip.SIP_PROFILE, sip_login="office.agent", sip_ip="198.51.100.10"),
                db,
            )
            spoofed = multi_sip.resolve_client(
                conn,
                SimpleNamespace(profile="internal", sip_login="office.agent", sip_ip="198.51.100.10"),
                db,
            )
        self.assertEqual(valid["id"], client_id)
        self.assertIsNone(spoofed)
        self.http.patch(
            f"/api/client-sip-credentials/{credential['id']}",
            json={"active": False},
            headers={"X-Money-Scale": str(db.MONEY_SCALE)},
        )
        with db.get_conn() as conn:
            disabled = multi_sip.resolve_client(
                conn,
                SimpleNamespace(profile=multi_sip.SIP_PROFILE, sip_login="office.agent", sip_ip="198.51.100.10"),
                db,
            )
        self.assertIsNone(disabled)

    def test_password_regeneration_and_freeswitch_feed(self):
        client_id = self.create_sip_client()
        credential = self.create_credential(client_id)
        response = self.http.post(
            f"/api/client-sip-credentials/{credential['id']}/regenerate-password",
            headers={"X-Money-Scale": str(db.MONEY_SCALE)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()["sip_password"], credential["sip_password"])
        feed = self.http.get("/api/freeswitch/sip-credentials").json()
        self.assertEqual(len(feed["credentials"]), 1)
        self.assertEqual(feed["credentials"][0]["client_id"], client_id)

    def test_login_validation_and_uniqueness(self):
        client_id = self.create_sip_client()
        self.assertEqual(
            self.http.post(
                f"/api/clients/{client_id}/sip-credentials",
                json={"sip_login": "bad login"},
                headers={"X-Money-Scale": str(db.MONEY_SCALE)},
            ).status_code,
            400,
        )
        self.create_credential(client_id, login="agent-100")
        self.assertEqual(
            self.http.post(
                f"/api/clients/{client_id}/sip-credentials",
                json={"sip_login": "agent-100"},
                headers={"X-Money-Scale": str(db.MONEY_SCALE)},
            ).status_code,
            409,
        )


if __name__ == "__main__":
    unittest.main()
