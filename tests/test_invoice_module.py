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

        self.assertEqual(september["rows"][0]["active_days"], 19)
        self.assertEqual(september["summary"]["mrc_cents"], 19000)
        self.assertEqual(september["summary"]["nrc_cents"], 5000)
        self.assertEqual(september["summary"]["amount_cents"], 24000)
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


if __name__ == "__main__":
    unittest.main()
