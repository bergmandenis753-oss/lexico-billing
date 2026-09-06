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
import terminator_balance_patch as supplier


class TerminatorAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_path = patch.object(db, "DB_PATH", Path(self.temp.name) / "billing.db")
        db_path.start()
        self.addCleanup(db_path.stop)
        credit_limit_common.ensure_schema(db)
        supplier.ensure_schema(db)
        self.app = FastAPI()
        credit_limit_calls.install_call_routes(self.app, main, db)
        supplier.install(self.app, main, db)
        self.app.dependency_overrides[main.require_api_key] = lambda: True
        self.app.dependency_overrides[main.require_admin] = lambda: "test"
        self.app.dependency_overrides[main.require_current_dashboard] = lambda: True
        self.http = TestClient(self.app)
        self.addCleanup(self.http.close)
        with db.get_conn() as conn:
            conn.execute("INSERT INTO clients (id, name, sip_ip, balance_cents) VALUES (1, 'test', '192.0.2.1', 1000000)")
            conn.execute("INSERT INTO termination_groups (id, name, ips) VALUES (28, 'ITD', '178.22.14.7')")
            conn.execute("INSERT INTO termination_groups (id, name) VALUES (29, 'Other supplier')")
            conn.execute(
                "INSERT INTO terminators (id, name, gateway_group_id, destination_name, prefix, gateway_name, cost_rate_cents) "
                "VALUES (84, 'IDT', 28, 'Czech Republic', '420', '', 1400)"
            )
        self.payload = dict(client_id=1, call_uuid="test-call-1", destination="420123456789",
                            terminator_id=84, terminator_name="IDT", route_ip="178.22.14.7",
                            billsec=807, sell_rate_cents=1570, cost_rate_cents=1400)

    def query(self, sql, args=()):
        conn = db.get_conn()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def balance(self, gid=28):
        return self.query("SELECT balance_cents FROM termination_groups WHERE id = ?", (gid,))[0][0]

    def finalize(self, **changes):
        return self.http.post("/api/finalize", json=self.payload | changes)

    def test_screenshot_call_posts_actual_cost_to_account_not_route_name(self):
        response = self.finalize()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["charged_cents"], 21117)
        self.assertEqual(result["margin_cents"], 2287)
        self.assertEqual(result["supplier_charge"]["cost_cents"], 18830)
        self.assertEqual(self.balance(), -18830)
        self.assertEqual(self.balance(29), 0)
        self.assertEqual(len(self.query("SELECT * FROM termination_group_call_charges")), 1)

    def test_retry_does_not_charge_client_or_supplier_twice(self):
        first = self.finalize().json()
        second = self.finalize().json()
        self.assertTrue(second["already_finalized"])
        self.assertEqual(second["balance_cents"], first["balance_cents"])
        self.assertEqual(self.balance(), -18830)
        self.assertEqual(len(self.query("SELECT * FROM cdr")), 1)

    def test_conflicting_retry_is_rejected_without_charges(self):
        self.finalize()
        self.assertEqual(self.finalize(billsec=900).status_code, 409)
        self.assertEqual(self.balance(), -18830)

    def test_concurrent_retries_are_atomic(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: self.finalize().status_code, range(8)))
        self.assertEqual(statuses, [200] * 8)
        self.assertEqual(self.balance(), -18830)
        self.assertEqual(len(self.query("SELECT * FROM cdr")), 1)

    def test_concurrent_distinct_calls_accumulate(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda i: self.finalize(call_uuid=f"distinct-{i}").status_code, range(8)))
        self.assertEqual(statuses, [200] * 8)
        self.assertEqual(self.balance(), -18830 * 8)
        self.assertEqual(len(self.query("SELECT * FROM termination_group_call_charges")), 8)

    def test_unanswered_call_and_zero_cost_do_not_debit(self):
        self.assertEqual(self.finalize(billsec=0).status_code, 200)
        self.assertEqual(self.finalize(call_uuid="free-route", cost_rate_cents=0).status_code, 200)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(len(self.query("SELECT * FROM termination_group_call_charges")), 0)

    def test_supplier_billing_cycle_is_independent_of_sell_cycle(self):
        result = self.finalize(billsec=6, cost_billing_cycle="60/60").json()
        self.assertEqual(result["supplier_charge"]["cost_cents"], 1400)
        self.assertEqual(result["charged_cents"], 157)
        self.assertEqual(self.balance(), -1400)

    def test_supplier_cost_is_not_clamped_to_client_credit(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE clients SET balance_cents = 10, credit_limit_cents = 20 WHERE id = 1")
        result = self.finalize().json()
        self.assertEqual(result["charged_cents"], 30)
        self.assertEqual(result["balance_cents"], -20)
        self.assertEqual(self.balance(), -18830)

    def test_failure_rolls_back_both_balances_and_cdr(self):
        real_post = supplier.post_call_cost

        def fail_after_post(conn, cdr_id):
            real_post(conn, cdr_id)
            raise sqlite3.OperationalError("simulated failure after supplier debit")

        with patch.object(credit_limit_calls, "post_call_cost", fail_after_post):
            with self.assertRaises(sqlite3.OperationalError):
                self.finalize()
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.query("SELECT balance_cents FROM clients WHERE id = 1")[0][0], 1000000)
        self.assertEqual(len(self.query("SELECT * FROM cdr")), 0)
        self.assertEqual(len(self.query("SELECT * FROM termination_group_call_charges")), 0)

    def test_manual_topups_remain_additive(self):
        response = self.http.post("/api/termination-groups/28/balance-adjust",
                                  json={"amount_cents": 10000, "note": "test topup"})
        self.assertEqual(response.status_code, 200)
        self.finalize()
        self.assertEqual(self.balance(), 10000 - 18830)
        self.assertEqual(len(self.query("SELECT * FROM termination_group_balance_adjustments")), 1)

    def test_schema_upgrade_preserves_balances_and_does_not_backfill(self):
        with patch.object(credit_limit_calls, "post_call_cost", return_value={"status": "legacy"}):
            self.finalize()
        with db.get_conn() as conn:
            conn.execute("UPDATE termination_groups SET balance_cents = 12345 WHERE id = 28")
        supplier.ensure_schema(db)
        supplier.ensure_schema(db)
        self.assertEqual(self.balance(), 12345)
        self.assertEqual(len(self.query("SELECT * FROM termination_group_call_charges")), 0)
        self.assertTrue(self.finalize().json()["already_finalized"])
        self.assertEqual(self.balance(), 12345)

    def test_ledger_replay_and_rate_edits_do_not_reprice_old_call(self):
        self.finalize()
        with db.get_conn() as conn:
            conn.execute("UPDATE terminators SET cost_rate_cents = 99999 WHERE id = 84")
            cdr_id = conn.execute("SELECT id FROM cdr").fetchone()[0]
            self.assertEqual(supplier.post_call_cost(conn, cdr_id)["status"], "already_posted")
        self.assertEqual(self.balance(), -18830)

    def test_unmapped_route_is_reported_not_charged_to_name_match(self):
        result = self.finalize(terminator_id=999).json()
        self.assertEqual(result["supplier_charge"]["status"], "unmapped")
        self.assertEqual(self.balance(), 0)

    def test_empty_uuid_is_rejected(self):
        self.assertEqual(self.finalize(call_uuid=" ").status_code, 422)
        self.assertEqual(len(self.query("SELECT * FROM cdr")), 0)


if __name__ == "__main__":
    unittest.main()
