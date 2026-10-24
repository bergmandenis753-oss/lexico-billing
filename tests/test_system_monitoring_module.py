import tempfile
import time
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
        system_monitoring_module._last_reservation_reconcile = 0.0
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

    def test_reconciles_only_old_reservations_after_both_nodes_report(self):
        conn = db.get_conn()
        try:
            client_id = conn.execute(
                "INSERT INTO clients (name, sip_ip) VALUES ('Test', '192.0.2.10')"
            ).lastrowid
            conn.execute(
                "INSERT INTO reservations (client_id, call_uuid, reserved_cents, expires_at, created_at) "
                "VALUES (?, 'live-primary', 10, strftime('%s','now') + 3600, datetime('now','-5 minutes'))",
                (client_id,),
            )
            conn.execute(
                "INSERT INTO reservations (client_id, call_uuid, reserved_cents, expires_at, created_at) "
                "VALUES (?, 'stale-call', 10, strftime('%s','now') + 3600, datetime('now','-5 minutes'))",
                (client_id,),
            )
            conn.execute(
                "INSERT INTO reservations (client_id, call_uuid, reserved_cents, expires_at, created_at) "
                "VALUES (?, 'setup-call', 10, strftime('%s','now') + 3600, datetime('now'))",
                (client_id,),
            )
            conn.commit()
        finally:
            conn.close()

        first = system_monitoring_module.record_sample(
            db,
            system_monitoring_module.SystemMetricIn(
                voice_node="primary", cpu_percent=10, active_call_uuids=["live-primary"]
            ),
        )
        self.assertEqual(first["reconciled_reservations"], 0)

        second = system_monitoring_module.record_sample(
            db,
            system_monitoring_module.SystemMetricIn(
                voice_node="secondary", cpu_percent=10, active_call_uuids=[]
            ),
        )
        self.assertEqual(second["reconciled_reservations"], 1)
        conn = db.get_conn()
        try:
            remaining = {
                row["call_uuid"]
                for row in conn.execute("SELECT call_uuid FROM reservations").fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(remaining, {"live-primary", "setup-call"})

    def test_empty_database_reports_offline_without_fake_zero_quality(self):
        result = system_monitoring_module.system_snapshot(db)

        self.assertFalse(result["online"])
        self.assertEqual(len(result["nodes"]), 2)
        for node in result["nodes"]:
            self.assertIsNone(node["latest"])
            self.assertIsNone(node["quality"]["packet_loss_percent"])
            self.assertIsNone(node["quality"]["jitter_ms"])

    def test_schema_upgrade_keeps_old_samples_on_primary_node(self):
        conn = system_monitoring_module._monitoring_conn(db)
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
        conn = system_monitoring_module._monitoring_conn(db)
        try:
            row = conn.execute(
                "SELECT voice_node, active_calls FROM system_metric_samples"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["voice_node"], "primary")
        self.assertEqual(row["active_calls"], 3)

    def test_monitoring_ingest_does_not_wait_for_billing_writer(self):
        billing_conn = db.get_conn()
        try:
            billing_conn.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            result = system_monitoring_module.record_sample(
                db,
                system_monitoring_module.SystemMetricIn(
                    voice_node="primary",
                    cpu_percent=12,
                    active_calls=3,
                ),
            )
            elapsed = time.monotonic() - started
        finally:
            billing_conn.rollback()
            billing_conn.close()

        self.assertTrue(result["ok"])
        self.assertLess(elapsed, 0.5)
        conn = db.get_conn()
        try:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_metric_samples'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(table)

    def test_incident_bundle_lifecycle_and_notifications(self):
        created = system_monitoring_module.create_incident(
            db,
            system_monitoring_module.SystemIncidentIn(
                voice_node="secondary",
                trigger_reason="RTP packet loss 8.50%",
                suspected_cause="network path",
                cpu_percent=44,
                active_calls=31,
                packet_loss_percent=8.5,
                jitter_ms=22,
                rtp_packets=1200,
                rtp_legs=4,
            ),
        )
        incident_id = created["incident_id"]

        pending = system_monitoring_module.pending_incidents(db)
        self.assertEqual(pending[0]["id"], incident_id)
        self.assertNotIn("bundle_path", pending[0])

        result = system_monitoring_module.store_incident_bundle(
            db, incident_id, b"test diagnostic archive"
        )
        self.assertEqual(result["bundle_size"], 23)
        row = system_monitoring_module._incident_row(db, incident_id)
        self.assertEqual(row["status"], "ready")
        self.assertTrue(Path(row["bundle_path"]).is_file())

        system_monitoring_module.mark_incident_notified(db, incident_id, "alert")
        system_monitoring_module.mark_incident_notified(db, incident_id, "bundle")
        self.assertEqual(system_monitoring_module.pending_incidents(db), [])

    def test_manual_vendor_capture_is_claimed_by_selected_node(self):
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO termination_groups (name, ips, gateway_name, active)
                   VALUES ('One', '91.22.4.250, 91.22.4.251', 'one-gw', 1)"""
            )
            conn.commit()
        finally:
            conn.close()

        created = system_monitoring_module.create_manual_capture(
            db,
            system_monitoring_module.ManualCaptureIn(
                voice_node="secondary",
                vendor_name="One",
            ),
        )
        request_id = created["capture_request_id"]

        self.assertIsNone(
            system_monitoring_module.claim_manual_capture(db, "primary")["command"]
        )
        command = system_monitoring_module.claim_manual_capture(db, "secondary")["command"]
        self.assertEqual(command["id"], request_id)
        self.assertEqual(command["vendor_ips"], ["91.22.4.250", "91.22.4.251"])
        self.assertEqual(command["gateway_names"], ["one-gw"])
        self.assertEqual(command["duration_seconds"], 600)
        self.assertEqual(command["max_recordings"], 2)

        system_monitoring_module.finish_manual_capture(
            db,
            request_id,
            system_monitoring_module.CaptureCommandResultIn(incident_id=42),
        )
        snapshot = system_monitoring_module.system_snapshot(db)
        self.assertEqual(snapshot["captures"][0]["status"], "complete")
        self.assertEqual(snapshot["captures"][0]["incident_id"], 42)
        self.assertEqual(snapshot["vendors"][0]["name"], "One")


if __name__ == "__main__":
    unittest.main()
