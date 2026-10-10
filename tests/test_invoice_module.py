import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

import db
import did_module
import invoice_module


class InvoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "billing.db"
        db.init_db()
        did_module.init_schema(db)
        conn = db.get_conn()
        self.traffic_client_id = conn.execute(
            "INSERT INTO clients (name, sip_ip, balance_cents, currency) "
            "VALUES ('Traffic Client', '198.51.100.10', 0, 'USD')"
        ).lastrowid
        self.did_client_id = conn.execute(
            "INSERT INTO did_clients (name, currency) VALUES ('DID Global', 'USD')"
        ).lastrowid
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tmp.cleanup()

    def test_traffic_invoice_uses_inclusive_dates_and_groups_directions(self):
        conn = db.get_conn()
        rows = [
            ("2026-09-01 00:00:00", "Belgium", 60, 1200),
            ("2026-09-30 23:59:59", "Belgium", 120, 2400),
            ("2026-09-15 12:00:00", "France", 0, 300),
            ("2026-10-01 00:00:00", "Belgium", 600, 12000),
        ]
        conn.executemany(
            """INSERT INTO cdr
                   (client_id, terminator_destination_name, billsec, sell_rate_cents,
                    cost_rate_cents, charged_cents, margin_cents, started_at)
               VALUES (?, ?, ?, 0, 0, ?, 0, ?)""",
            [(self.traffic_client_id, direction, seconds, amount, started)
             for started, direction, seconds, amount in rows],
        )
        conn.commit()

        result = invoice_module.traffic_invoice(
            conn, self.traffic_client_id, "2026-09-01", "2026-09-30"
        )
        conn.close()

        self.assertEqual(result["summary"]["calls"], 3)
        self.assertEqual(result["summary"]["answered_calls"], 2)
        self.assertEqual(result["summary"]["seconds"], 180)
        self.assertEqual(result["summary"]["amount_cents"], 3900)
        by_direction = {row["direction"]: row for row in result["rows"]}
        self.assertEqual(by_direction["Belgium"]["amount_cents"], 3600)
        self.assertEqual(by_direction["France"]["calls"], 1)

    def test_did_invoice_prorates_mrc_and_charges_nrc_once(self):
        conn = db.get_conn()
        conn.execute(
            """INSERT INTO did_numbers
                   (client_id, did_number, destination, sold_on, sell_mrc_cents,
                    sell_nrc_cents, cost_mrc_cents, cost_nrc_cents)
               VALUES (?, '420210012333', '198.51.100.20:5060', '2026-09-12',
                       30000, 5000, 12000, 2000)""",
            (self.did_client_id,),
        )
        conn.commit()

        september = invoice_module.did_invoice(
            conn, self.did_client_id, "2026-09-01", "2026-09-30"
        )
        october = invoice_module.did_invoice(
            conn, self.did_client_id, "2026-10-01", "2026-10-31"
        )
        conn.close()

        self.assertEqual(september["rows"][0]["billing_events"], 1)
        self.assertEqual(september["summary"]["mrc_cents"], 30000)
        self.assertEqual(september["summary"]["nrc_cents"], 5000)
        self.assertEqual(september["summary"]["amount_cents"], 35000)
        self.assertEqual(october["summary"]["mrc_cents"], 30000)
        self.assertEqual(october["summary"]["nrc_cents"], 0)
        self.assertNotIn("cost_mrc_cents", september["rows"][0])
        self.assertNotIn("cost_nrc_cents", september["rows"][0])

    def test_reversed_period_is_rejected(self):
        conn = db.get_conn()
        with self.assertRaises(HTTPException) as error:
            invoice_module.traffic_invoice(
                conn, self.traffic_client_id, "2026-10-02", "2026-10-01"
            )
        conn.close()
        self.assertEqual(error.exception.status_code, 422)

    def test_invoice_options_include_inactive_clients(self):
        conn = db.get_conn()
        conn.execute(
            "UPDATE clients SET active = 0 WHERE id = ?", (self.traffic_client_id,)
        )
        conn.execute(
            "UPDATE did_clients SET active = 0 WHERE id = ?", (self.did_client_id,)
        )
        conn.commit()

        result = invoice_module.invoice_options_data(conn, db.MONEY_SCALE)
        conn.close()

        self.assertEqual(result["traffic_clients"][0]["id"], self.traffic_client_id)
        self.assertEqual(result["traffic_clients"][0]["active"], 0)
        self.assertEqual(result["did_clients"][0]["id"], self.did_client_id)
        self.assertEqual(result["did_clients"][0]["active"], 0)

    def test_add_did_from_invoice_creates_safe_billing_record_and_updates_it(self):
        conn = db.get_conn()
        batch = invoice_module.save_did_sales(conn, invoice_module.DidSaleBatchIn(
            client_id=self.did_client_id,
            did_numbers=["+420 210 012 333", "420210014180", "420210014180"],
            sold_on="2026-09-12",
            sell_mrc_cents=22000,
            sell_nrc_cents=22000,
            cost_mrc_cents=15000,
            cost_nrc_cents=15000,
        ))
        conn.commit()
        self.assertEqual(batch["count"], 2)
        self.assertEqual(batch["created"], 2)
        created = batch["items"][0]
        row = conn.execute(
            "SELECT * FROM did_numbers WHERE id = ?", (created["id"],)
        ).fetchone()
        self.assertEqual(row["did_number"], "420210012333")
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["destination"], "invoice-only")
        self.assertEqual(row["cost_mrc_cents"], 15000)

        updated = invoice_module.save_did_sale(conn, invoice_module.DidSaleIn(
            client_id=self.did_client_id,
            did_number="420210012333",
            sold_on="2026-09-13",
            sell_mrc_cents=25000,
            sell_nrc_cents=10000,
            cost_mrc_cents=16000,
            cost_nrc_cents=9000,
        ))
        conn.commit()
        changed = conn.execute(
            "SELECT * FROM did_numbers WHERE id = ?", (created["id"],)
        ).fetchone()
        conn.close()
        self.assertFalse(updated["created"])
        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(changed["sold_on"], "2026-09-13")
        self.assertEqual(changed["sell_mrc_cents"], 25000)
        self.assertEqual(changed["cost_nrc_cents"], 9000)


if __name__ == "__main__":
    unittest.main()
