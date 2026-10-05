import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import credit_limit_calls
import db
import main
import route_number_whitelist


class RouteNumberWhitelistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "BILLING_DB_PATH": str(Path(self.temp.name) / "billing.db"),
            "ADMIN_USER": "test",
            "ADMIN_PASSWORD": "test-password",
            "API_SECRET_KEY": "test-api-key",
        })
        env.start()
        self.addCleanup(env.stop)
        database = patch.object(db, "DB_PATH", Path(self.temp.name) / "billing.db")
        database.start()
        self.addCleanup(database.stop)
        db.init_db()
        with db.get_conn() as conn:
            conn.execute("INSERT INTO clients (id, name, sip_ip, balance_cents) VALUES (1, 'Global', '192.0.2.1', 100000)")
            conn.execute("INSERT INTO terminators (id, name, destination_name, prefix, gateway_name, cost_rate_cents) VALUES (1, 'One', 'Poland', '48', 'one', 1000)")
            conn.execute("INSERT INTO client_rates (id, client_id, terminator_id, client_tech_prefix, prefix, destination_name, sell_rate_cents) VALUES (1, 1, 1, '777', '48', 'Poland', 2000)")
        app = FastAPI()
        app.include_router(main.app.router)
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") not in {"/api/reserve", "/api/finalize"}]
        credit_limit_calls.install_call_routes(app, main, db)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def save(self, kind, enabled, numbers):
        return self.http.put(
            f"/api/client-rates/1/{kind}-number-whitelist",
            json={"enabled": enabled, "numbers": numbers},
            auth=("test", "test-password"),
            headers={"X-Money-Scale": str(db.MONEY_SCALE)},
        )

    def reserve(self, destination="77748123456789", clid="12025550109", uuid="call"):
        return self.http.post(
            "/api/reserve",
            headers={"X-Api-Key": "test-api-key"},
            json={"sip_ip": "192.0.2.1", "destination": destination, "clid": clid, "call_uuid": uuid},
        )

    def test_a_and_b_lists_block_before_reservation(self):
        response = self.save("a", True, "+1 (202) 555-0109")
        self.assertEqual(response.status_code, 200, response.text)
        allowed = self.reserve(uuid="allowed-a")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        blocked_a = self.reserve(clid="12025550110", uuid="blocked-a")
        self.assertEqual(blocked_a.status_code, 503, blocked_a.text)
        malformed_a = self.reserve(clid="1202bad5550109", uuid="malformed-a")
        self.assertEqual(malformed_a.status_code, 503, malformed_a.text)

        self.assertEqual(self.save("a", False, "12025550109").status_code, 200)
        self.assertEqual(self.save("b", True, "0048 123 456 789").status_code, 200)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM reservations")
        allowed_b = self.reserve(uuid="allowed-b")
        self.assertEqual(allowed_b.status_code, 200, allowed_b.text)
        blocked_b = self.reserve(destination="77748999999999", uuid="blocked-b")
        self.assertEqual(blocked_b.status_code, 503, blocked_b.text)

        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 1)
            blocked = conn.execute("SELECT stage FROM sip_hits WHERE call_uuid = 'blocked-b'").fetchone()
            self.assertEqual(blocked["stage"], "b_number_whitelist")

    def test_disabled_lists_do_not_change_existing_routing(self):
        self.assertEqual(self.save("a", False, "12025550109").status_code, 200)
        self.assertEqual(self.save("b", False, "48123456789").status_code, 200)
        self.assertEqual(self.reserve(clid="19999999999").status_code, 200)

    def test_validation_and_repeatable_migration(self):
        self.assertEqual(self.save("a", True, "not-a-number").status_code, 422)
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE client_rates (id INTEGER PRIMARY KEY)")
        route_number_whitelist.init_schema(conn)
        route_number_whitelist.init_schema(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(client_rates)")}
        self.assertTrue(set(sum((list(value) for value in route_number_whitelist.KINDS.values()), [])).issubset(columns))


if __name__ == "__main__":
    unittest.main()
