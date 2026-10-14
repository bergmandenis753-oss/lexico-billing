import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import db
import system_monitoring_module


class SystemMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "billing.db"
        db.init_db()
        system_monitoring_module.init_schema(db)

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tmp.cleanup()

    def test_records_latest_and_weighted_rtp_quality(self):
        now = datetime.now(timezone.utc).isoformat()
        system_monitoring_module.record_sample(db, system_monitoring_module.SystemMetricIn(
            sampled_at=now,
            cpu_percent=52.5,
            cpu_steal_percent=7.0,
            active_calls=48,
            packet_loss_percent=1.0,
            jitter_ms=10.0,
            rtp_packets=100,
            rtp_legs=2,
        ))
        system_monitoring_module.record_sample(db, system_monitoring_module.SystemMetricIn(
            sampled_at=now,
            cpu_percent=64.0,
            active_calls=51,
            packet_loss_percent=3.0,
            jitter_ms=20.0,
            rtp_packets=300,
            rtp_legs=4,
        ))

        result = system_monitoring_module.system_snapshot(db)

        self.assertTrue(result["online"])
        self.assertEqual(result["latest"]["active_calls"], 51)
        self.assertEqual(result["quality"]["packet_loss_percent"], 2.5)
        self.assertEqual(result["quality"]["jitter_ms"], 17.5)
        self.assertEqual(result["quality"]["rtp_packets"], 400)
        self.assertEqual(result["quality"]["rtp_legs"], 6)

    def test_empty_database_reports_offline_without_fake_zero_quality(self):
        result = system_monitoring_module.system_snapshot(db)

        self.assertFalse(result["online"])
        self.assertIsNone(result["latest"])
        self.assertIsNone(result["quality"]["packet_loss_percent"])
        self.assertIsNone(result["quality"]["jitter_ms"])


if __name__ == "__main__":
    unittest.main()
