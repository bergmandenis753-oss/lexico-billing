import sqlite3
import unittest
from pathlib import Path

import admin_management_patch
import client_route_isolation_patch


class ClientRoutingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE client_rates ("
            "id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, terminator_id INTEGER, "
            "client_tech_prefix TEXT NOT NULL DEFAULT '', prefix TEXT NOT NULL, "
            "destination_name TEXT NOT NULL, sell_rate_cents INTEGER NOT NULL, "
            "billing_cycle TEXT NOT NULL DEFAULT '1/1')"
        )

    def tearDown(self):
        self.conn.close()

    def test_route_upsert_preserves_selected_billing_cycle(self):
        payload = {
            "client_id": 7,
            "terminator_id": 12,
            "client_tech_prefix": "103",
            "prefix": "32",
            "destination_name": "Belgium",
            "sell_rate_cents": 900,
            "billing_cycle": "60/1",
        }
        route_id, created = client_route_isolation_patch._upsert_client_route(self.conn, payload)
        self.assertTrue(created)
        row = self.conn.execute("SELECT * FROM client_rates WHERE id = ?", (route_id,)).fetchone()
        self.assertEqual(row["billing_cycle"], "60/1")

        payload["sell_rate_cents"] = 1000
        payload["billing_cycle"] = "1/1"
        same_id, created = client_route_isolation_patch._upsert_client_route(self.conn, payload)
        self.assertFalse(created)
        self.assertEqual(same_id, route_id)
        row = self.conn.execute("SELECT * FROM client_rates WHERE id = ?", (route_id,)).fetchone()
        self.assertEqual(row["sell_rate_cents"], 1000)
        self.assertEqual(row["billing_cycle"], "1/1")

    def test_route_patch_accepts_billing_cycle(self):
        data = admin_management_patch.ClientRatePatchIn(billing_cycle="60/1")
        self.assertEqual(data.billing_cycle, "60/1")

    def test_dashboard_has_client_filter_and_terminator_autofill(self):
        html = (Path(__file__).parents[1] / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="route-client-filter"', html)
        self.assertIn("function syncDirectionFromTerm", html)
        self.assertIn("openDirectionDlg(selectedRouteClientId)", html)
        self.assertIn("openRouteSettings", html)
        self.assertIn("openRouteNumberWhitelist('a')", html)
        self.assertIn("openRouteNumberWhitelist('b')", html)


if __name__ == "__main__":
    unittest.main()
