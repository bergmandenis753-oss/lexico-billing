import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

import db
import monitoring_module


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "billing.db"
        db.init_db()
        monitoring_module.init_schema(db)
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

    def test_period_snapshot_uses_inclusive_dates(self):
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO cdr
                   (client_id, destination, terminator_name, terminator_destination_name,
                    billsec, sell_rate_cents, cost_rate_cents, charged_cents,
                    margin_cents, started_at)
                   VALUES (?, 'Germany', 'One', 'Germany', 60, 3000, 2000,
                           3000, 1000, '2026-10-10 12:00:00')""",
                (self.client_id,),
            )
            conn.commit()
        finally:
            conn.close()

        result = monitoring_module.period_snapshot(
            db, "2026-10-10", "2026-10-10", force=True
        )

        self.assertEqual(result["date_from"], "2026-10-10")
        self.assertEqual(result["date_to"], "2026-10-10")
        self.assertEqual(result["summary"]["calls"], 1)
        self.assertEqual(result["rows"][0]["direction_name"], "Germany")

    def test_period_validation_rejects_reversed_and_oversized_ranges(self):
        with self.assertRaises(HTTPException):
            monitoring_module.normalized_period("2026-10-11", "2026-10-10")
        with self.assertRaises(HTTPException):
            monitoring_module.normalized_period("2025-01-01", "2026-10-10")

    def test_traffic_drop_alert_requires_peak_then_five_minutes_of_zero(self):
        start = 1_800_000_000
        conn = db.get_conn()
        try:
            rate_id = conn.execute(
                """INSERT INTO client_rates
                   (client_id, client_tech_prefix, prefix, destination_name,
                    sell_rate_cents, voice_node)
                   VALUES (?, '105', '49', 'Germany', 800, 'secondary')""",
                (self.client_id,),
            ).lastrowid
            conn.executemany(
                """INSERT INTO reservations
                   (client_id, client_rate_id, call_uuid, reserved_cents, expires_at)
                   VALUES (?, ?, ?, 1000, ?)""",
                [
                    (self.client_id, rate_id, f"call-{index}", start + 3600)
                    for index in range(9)
                ],
            )
            conn.commit()
        finally:
            conn.close()

        monitoring_module.evaluate_traffic_drop_alerts(
            db, start, threshold_calls=10, silence_seconds=300
        )
        conn = db.get_conn()
        try:
            tracker_count = conn.execute(
                "SELECT COUNT(*) AS c FROM traffic_drop_trackers"
            ).fetchone()["c"]
            self.assertEqual(tracker_count, 0)
            conn.execute(
                """INSERT INTO reservations
                   (client_id, client_rate_id, call_uuid, reserved_cents, expires_at)
                   VALUES (?, ?, 'call-9', 1000, ?)""",
                (self.client_id, rate_id, start + 3600),
            )
            conn.commit()
        finally:
            conn.close()

        monitoring_module.evaluate_traffic_drop_alerts(
            db, start, threshold_calls=10, silence_seconds=300
        )
        conn = db.get_conn()
        try:
            conn.execute("DELETE FROM reservations")
            conn.commit()
        finally:
            conn.close()

        monitoring_module.evaluate_traffic_drop_alerts(
            db, start + 30, threshold_calls=10, silence_seconds=300
        )
        monitoring_module.evaluate_traffic_drop_alerts(
            db, start + 329, threshold_calls=10, silence_seconds=300
        )
        self.assertEqual(monitoring_module.pending_traffic_drop_alerts(db, evaluate=False), [])

        monitoring_module.evaluate_traffic_drop_alerts(
            db, start + 330, threshold_calls=10, silence_seconds=300
        )
        alerts = monitoring_module.pending_traffic_drop_alerts(db, evaluate=False)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["client_name"], "Metavoip")
        self.assertEqual(alerts[0]["direction_name"], "Germany")
        self.assertEqual(alerts[0]["client_tech_prefix"], "105")
        self.assertEqual(alerts[0]["peak_calls"], 10)
        self.assertEqual(alerts[0]["voice_node"], "secondary")

        monitoring_module.evaluate_traffic_drop_alerts(
            db, start + 900, threshold_calls=10, silence_seconds=300
        )
        self.assertEqual(len(monitoring_module.pending_traffic_drop_alerts(db, evaluate=False)), 1)

        monitoring_module.mark_traffic_drop_alert_notified(db, alerts[0]["id"])
        self.assertEqual(monitoring_module.pending_traffic_drop_alerts(db, evaluate=False), [])


if __name__ == "__main__":
    unittest.main()
