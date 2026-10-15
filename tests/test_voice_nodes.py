import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import credit_limit_calls
import credit_limit_common
import db
import main
import voice_nodes


class VoiceNodeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(
            os.environ,
            {
                "BILLING_DB_PATH": str(Path(self.temp.name) / "billing.db"),
                "ADMIN_USER": "test",
                "ADMIN_PASSWORD": "test-password",
                "API_SECRET_KEY": "test-api-key",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        database = patch.object(db, "DB_PATH", Path(self.temp.name) / "billing.db")
        database.start()
        self.addCleanup(database.stop)
        db.init_db()
        credit_limit_common.ensure_schema(db)
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO clients (id, name, sip_ip, balance_cents) "
                "VALUES (1, 'Metavoip', '192.0.2.1', 10000000)"
            )
            conn.execute(
                "INSERT INTO terminators "
                "(id, name, destination_name, prefix, gateway_name, cost_rate_cents) "
                "VALUES (1, 'One', 'Poland', '48', '91.224.250.26', 100)"
            )
            conn.execute(
                "INSERT INTO client_rates "
                "(id, client_id, terminator_id, client_tech_prefix, prefix, destination_name, "
                "sell_rate_cents, voice_node) VALUES (1, 1, 1, '103', '48', 'Poland', 200, 'secondary')"
            )

        app = FastAPI()
        app.include_router(main.app.router)
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", "") not in {"/api/reserve", "/api/finalize"}
        ]
        credit_limit_calls.install_call_routes(app, main, db)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def reserve(self, node):
        return self.http.post(
            "/api/reserve",
            headers={"X-Api-Key": "test-api-key"},
            json={
                "sip_ip": "192.0.2.1",
                "destination": "10348501234567",
                "clid": "48500999888",
                "call_uuid": f"call-{node}",
                "voice_node": node,
            },
        )

    def test_only_assigned_node_can_reserve_route(self):
        blocked = self.reserve("primary")
        self.assertEqual(blocked.status_code, 503, blocked.text)
        self.assertIn("secondary", blocked.text)

        allowed = self.reserve("secondary")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["voice_node"], "secondary")

        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT voice_node, stage FROM sip_hits ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [(row["voice_node"], row["stage"]) for row in rows],
            [("primary", "voice_node"), ("secondary", "reserved")],
        )

    def test_migration_defaults_existing_routes_to_primary(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE client_rates SET voice_node = '' WHERE id = 1")
            voice_nodes.init_schema(conn)
            row = conn.execute("SELECT voice_node FROM client_rates WHERE id = 1").fetchone()
        self.assertEqual(row["voice_node"], "primary")


if __name__ == "__main__":
    unittest.main()
