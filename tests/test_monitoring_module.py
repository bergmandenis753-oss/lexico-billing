import tempfile
import unittest
from pathlib import Path

import db
import monitoring_module


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "billing.db"
        db.init_db()
        conn = db.get_conn()
        self.client_id = conn.execute(
            "INSERT INTO clients (name, sip_ip, balance_cents, currency) VALUES ('Metavoip', '198.51.100.2', 100000, 'USD')"
        ).lastrowid
        conn.executemany(
            """INSERT INTO cdr
               (client_id, destination, terminator_name, terminator_destination_name,
                billsec, sell_rate_cents, cost_rate_cents, charged_cents, margin_cents)
               VALUES (?, 'Poland', 'One', 'Poland', ?, 3000, 2000, ?, ?)""",
            [
                (self.client_id, 3600, 180000, 60000),
                (self.client_id, 2400, 120000, 40000),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tmp.cleanup()

    def test_daily_rows_are_aggregated_and_sorted(self):
        result = monitoring_module.daily_snapshot(db, force=True)
        self.assertEqual(result["summary"]["billsec"], 6000)
        self.assertEqual(result["summary"]["revenue_units"], 300000)
        self.assertEqual(result["summary"]["cost_units"], 200000)
        self.assertEqual(result["summary"]["margin_units"], 100000)
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["client_name"], "Metavoip")
        self.assertEqual(row["direction_name"], "Poland")
        self.assertEqual(row["terminator_name"], "One")
        self.assertEqual(row["average_sell_rate_units"], 3000)
        self.assertEqual(row["average_cost_rate_units"], 2000)


if __name__ == "__main__":
    unittest.main()
