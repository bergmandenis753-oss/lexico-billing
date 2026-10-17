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
            voice_node="secondary",
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
            voice_node="secondary",
            cpu_percent=64.0,
            active_calls=51,
            packet_loss_percent=3.0,
            jitter_ms=20.0,
            rtp_packets=300,
            rtp_legs=4,
        ))

        result = system_monitoring_module.system_snapshot(db)
        secondary = next(node for node in result["nodes"] if node["id"] == "secondary")

        self.assertTrue(result["online"])
        self.assertFalse(result["all_online"])
        self.assertEqual(secondary["latest"]["active_calls"], 51)
        self.assertEqual(secondary["quality"]["packet_loss_percent"], 2.5)
        self.assertEqual(secondary["quality"]["jitter_ms"], 17.5)
        self.assertEqual(secondary["quality"]["rtp_packets"], 400)
        self.assertEqual(secondary["quality"]["rtp_legs"], 6)

    def test_each_voice_server_has_independent_metrics(self):
        now = datetime.now(timezone.utc).isoformat()
        for node, cpu, calls, loss, jitter in (
            ("primary", 21.0, 12, 0.4, 8.0),
            ("secondary", 66.0, 37, 2.1, 24.0),
        ):
            system_monitoring_module.record_sample(db, system_monitoring_module.SystemMetricIn(
                sampled_at=now,
                voice_node=node,
                cpu_percent=cpu,
                active_calls=calls,
                packet_loss_percent=loss,
                jitter_ms=jitter,
                rtp_packets=100,
                rtp_legs=1,
            ))

        result = system_monitoring_module.system_snapshot(db)
        snapshots = {node["id"]: node for node in result["nodes"]}

        self.assertTrue(result["all_online"])
        self.assertEqual(snapshots["primary"]["latest"]["active_calls"], 12)
        self.assertEqual(snapshots["secondary"]["latest"]["active_calls"], 37)
        self.assertEqual(snapshots["primary"]["quality"]["jitter_ms"], 8.0)
        self.assertEqual(snapshots["secondary"]["quality"]["jitter_ms"], 24.0)

    def test_empty_database_reports_offline_without_fake_zero_quality(self):
        result = system_monitoring_module.system_snapshot(db)

        self.assertFalse(result["online"])
        self.assertEqual(len(result["nodes"]), 2)
        for node in result["nodes"]:
            self.assertIsNone(node["latest"])
            self.assertIsNone(node["quality"]["packet_loss_percent"])
            self.assertIsNone(node["quality"]["jitter_ms"])

    def test_schema_upgrade_keeps_old_samples_on_primary_node(self):
        conn = db.get_conn()
        try:
            conn.execute("DROP TABLE system_metric_samples")
            conn.execute(
                """CREATE TABLE system_metric_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'voip',
                    cpu_percent REAL NOT NULL,
                    cpu_steal_percent REAL NOT NULL DEFAULT 0,
                    active_calls INTEGER NOT NULL DEFAULT 0,
                    packet_loss_percent REAL,
                    jitter_ms REAL,
                    rtp_packets INTEGER NOT NULL DEFAULT 0,
                    rtp_legs INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.execute(
                "INSERT INTO system_metric_samples "
                "(sampled_at, cpu_percent, active_calls) VALUES (datetime('now'), 12.5, 3)"
            )
            conn.commit()
        finally:
            conn.close()

        system_monitoring_module.init_schema(db)
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT voice_node, active_calls FROM system_metric_samples"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["voice_node"], "primary")
        self.assertEqual(row["active_calls"], 3)


if __name__ == "__main__":
    unittest.main()
