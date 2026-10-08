import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import credit_limit_calls
import credit_limit_common
import db
import main
import route_line_limit


class RouteLineLimitTests(unittest.TestCase):
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
                "VALUES (1, 'IDT', 'Portugal', '351', 'idt', 300)"
            )
            conn.execute(
                "INSERT INTO terminators "
                "(id, name, destination_name, prefix, gateway_name, cost_rate_cents) "
                "VALUES (2, 'IDT', 'Germany', '49', 'idt', 300)"
            )
            conn.execute(
                "INSERT INTO client_rates "
                "(id, client_id, terminator_id, client_tech_prefix, prefix, destination_name, "
                "sell_rate_cents, line_limit_enabled, line_limit) "
                "VALUES (1, 1, 1, '103', '351', 'Portugal', 670, 1, 2)"
            )
            conn.execute(
                "INSERT INTO client_rates "
                "(id, client_id, terminator_id, client_tech_prefix, prefix, destination_name, "
                "sell_rate_cents, line_limit_enabled, line_limit) "
                "VALUES (2, 1, 2, '103', '49', 'Germany', 670, 1, 1)"
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

    def reserve(self, uuid, destination="103351210000000"):
        return self.http.post(
            "/api/reserve",
            headers={"X-Api-Key": "test-api-key"},
            json={
                "sip_ip": "192.0.2.1",
                "destination": destination,
                "clid": "351910000000",
                "call_uuid": uuid,
            },
        )

    def test_limit_is_per_route_and_same_call_is_idempotent(self):
        self.assertEqual(self.reserve("portugal-1").status_code, 200)
        self.assertEqual(self.reserve("portugal-2").status_code, 200)
        blocked = self.reserve("portugal-3")
        self.assertEqual(blocked.status_code, 503, blocked.text)
        self.assertIn("2/2", blocked.text)

        repeated = self.reserve("portugal-1")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        germany = self.reserve("germany-1", "1034915112345678")
        self.assertEqual(germany.status_code, 200, germany.text)

        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT call_uuid, client_rate_id FROM reservations ORDER BY call_uuid"
            ).fetchall()
            self.assertEqual(
                [(row["call_uuid"], row["client_rate_id"]) for row in rows],
                [("germany-1", 2), ("portugal-1", 1), ("portugal-2", 1)],
            )
            hit = conn.execute(
                "SELECT stage FROM sip_hits WHERE call_uuid = 'portugal-3'"
            ).fetchone()
            self.assertEqual(hit["stage"], "route_line_limit")

    def test_disabled_or_expired_reservations_do_not_block(self):
        self.assertEqual(self.reserve("first").status_code, 200)
        self.assertEqual(self.reserve("second").status_code, 200)
        with db.get_conn() as conn:
            conn.execute("UPDATE reservations SET expires_at = 0 WHERE call_uuid = 'first'")
        self.assertEqual(self.reserve("replacement").status_code, 200)

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE client_rates SET line_limit_enabled = 0 WHERE id = 1"
            )
        for index in range(3):
            response = self.reserve(f"unlimited-{index}")
            self.assertEqual(response.status_code, 200, response.text)

    def test_parallel_reservations_never_exceed_limit(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE client_rates SET line_limit = 3 WHERE id = 1")
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(self.reserve, [f"parallel-{i}" for i in range(8)]))
        self.assertEqual(sum(response.status_code == 200 for response in responses), 3)
        self.assertEqual(sum(response.status_code == 503 for response in responses), 5)
        with db.get_conn() as conn:
            active = route_line_limit.active_count(conn, 1, db.now())
        self.assertEqual(active, 3)

    def test_validation_and_repeatable_migration(self):
        self.assertEqual(route_line_limit.normalize(False, 0), (0, 0))
        self.assertEqual(route_line_limit.normalize(True, 5), (1, 5))
        for enabled, limit in ((True, 0), (True, -1), (True, 10001)):
            with self.assertRaises(ValueError):
                route_line_limit.normalize(enabled, limit)

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE client_rates (id INTEGER PRIMARY KEY);"
            "CREATE TABLE reservations (id INTEGER PRIMARY KEY, expires_at INTEGER);"
        )
        route_line_limit.init_schema(conn)
        route_line_limit.init_schema(conn)
        rate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(client_rates)")}
        reservation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(reservations)")
        }
        self.assertTrue({"line_limit_enabled", "line_limit"}.issubset(rate_columns))
        self.assertIn("client_rate_id", reservation_columns)


if __name__ == "__main__":
    unittest.main()
