import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import ops_entry
import telegram_standalone as bot


class CallCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "billing.db"
        db.init_db()
        self.conn = db.get_conn()
        client_id = self.conn.execute(
            "INSERT INTO clients (name, sip_ip) VALUES ('Metavoip', '195.26.237.168')"
        ).lastrowid
        self.conn.execute(
            """INSERT INTO sip_hits
                   (call_uuid, sip_ip, destination, client_id, client_name,
                    client_tech_prefix, dial_destination, provider_number,
                    gateway_name, terminator_name, terminator_destination_name,
                    status, stage, reason, sip_call_id)
               VALUES ('wanted-uuid', '195.26.237.168', '103998955601166', ?,
                       'Metavoip', '103', '998955601166', '6489998955601166',
                       'idt', 'IDT UZ', 'Uzbekistan', 'allowed', 'reserved',
                       'Холд поставлен', 'wanted-call-id')""",
            (client_id,),
        )
        self.conn.execute(
            """INSERT INTO sip_hits
                   (call_uuid, sip_ip, destination, client_id, client_name,
                    dial_destination, gateway_name, terminator_name, status, stage)
               VALUES ('other-uuid', '195.26.237.168', '996502874006', ?,
                       'Metavoip', '996502874006', 'lexico', 'Leixo',
                       'allowed', 'reserved')""",
            (client_id,),
        )
        self.conn.execute(
            """INSERT INTO cdr
                   (client_id, call_uuid, sip_ip, destination, client_tech_prefix,
                    dial_destination, provider_number, gateway_name, terminator_name,
                    terminator_destination_name, billsec, sell_rate_cents,
                    cost_rate_cents, charged_cents, margin_cents)
               VALUES (?, 'wanted-uuid', '195.26.237.168', '103998955601166',
                       '103', '998955601166', '6489998955601166', 'idt',
                       'IDT UZ', 'Uzbekistan', 0, 0, 0, 0, 0)""",
            (client_id,),
        )
        self.conn.execute(
            "INSERT INTO pcap_events (call_id, method) VALUES ('wanted-call-id', 'INVITE')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        db.DB_PATH = self.original_path
        self.tmp.cleanup()

    def test_call_check_returns_only_requested_b_number(self):
        result = ops_entry.call_check_context(self.conn, "+998 95 560 11 66")

        self.assertEqual(result["number"], "998955601166")
        self.assertEqual(len(result["sip_hits"]), 1)
        self.assertEqual(result["sip_hits"][0]["terminator_name"], "IDT UZ")
        self.assertEqual(result["cdr"][0]["dial_destination"], "998955601166")
        self.assertEqual(result["pcap_events"][0]["call_id"], "wanted-call-id")

    def test_telegram_check_never_falls_back_to_unrelated_latest_hit(self):
        report = ops_entry.call_check_context(self.conn, "998955601166")
        data = {
            "money_scale": db.MONEY_SCALE,
            "sip_hits": [{"destination": "996502874006", "terminator_name": "Leixo"}],
            "cdr": [],
            "pcap_events": [],
        }
        with mock.patch.object(bot, "_load_call_check", return_value=report):
            text, _ = bot._answer_for_text(data, "/check 998955601166")

        self.assertIn("998955601166", text)
        self.assertIn("IDT UZ", text)
        self.assertIn("Uzbekistan", text)
        self.assertNotIn("996502874006", text)
        self.assertNotIn("Leixo", text)

    def test_telegram_check_reports_missing_number_without_unrelated_hit(self):
        report = {"sip_hits": [], "cdr": [], "pcap_events": []}
        data = {"sip_hits": [{"destination": "996502874006"}], "cdr": []}
        with mock.patch.object(bot, "_load_call_check", return_value=report):
            text, _ = bot._answer_for_text(data, "/check 998000000000")

        self.assertIn("не найдены", text)
        self.assertNotIn("996502874006", text)


if __name__ == "__main__":
    unittest.main()
