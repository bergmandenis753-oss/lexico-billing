import asyncio
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
                   (call_uuid, sip_ip, destination, outbound_caller_id, client_id, client_name,
                    client_tech_prefix, dial_destination, provider_number,
                    gateway_name, terminator_name, terminator_destination_name,
                    status, stage, reason, sip_call_id, voice_node)
               VALUES ('wanted-uuid', '195.26.237.168', '103998955601166', '49111222333', ?,
                       'Metavoip', '103', '998955601166', '6489998955601166',
                       'idt', 'IDT UZ', 'Uzbekistan', 'allowed', 'reserved',
                       'Холд поставлен', 'wanted-call-id', 'secondary')""",
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
                    cost_rate_cents, charged_cents, margin_cents, voice_node)
               VALUES (?, 'wanted-uuid', '195.26.237.168', '103998955601166',
                       '103', '998955601166', '6489998955601166', 'idt',
                       'IDT UZ', 'Uzbekistan', 0, 0, 0, 0, 0, 'secondary')""",
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

    def test_call_check_exact_dial_destination_uses_index(self):
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM sip_hits WHERE dial_destination = ?",
            ("998955601166",),
        ).fetchall()

        self.assertTrue(any("idx_sip_hits_dial_destination" in row["detail"] for row in plan))

    def test_call_check_suffix_fallback_uses_real_table_with_alias(self):
        result = ops_entry.call_check_context(self.conn, "955601166")

        self.assertEqual(len(result["sip_hits"]), 1)
        self.assertEqual(result["sip_hits"][0]["call_uuid"], "wanted-uuid")
        self.assertEqual(result["cdr"][0]["call_uuid"], "wanted-uuid")

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
        self.assertIn("B-номер получен: 103998955601166", text)
        self.assertIn("A-номер поставщику: 49111222333", text)
        self.assertIn("Наш сервер: Новый · 207.154.219.145", text)
        self.assertNotIn("996502874006", text)
        self.assertNotIn("Leixo", text)

    def test_telegram_check_reports_voice_server_for_cdr_fallback(self):
        report = ops_entry.call_check_context(self.conn, "998955601166")
        report["sip_hits"] = []
        data = {"money_scale": db.MONEY_SCALE, "sip_hits": [], "cdr": [], "pcap_events": []}
        with mock.patch.object(bot, "_load_call_check", return_value=report):
            text, _ = bot._answer_for_text(data, "/check 998955601166")

        self.assertIn("Наш сервер: Новый · 207.154.219.145", text)

    def test_telegram_check_uses_outbound_invite_for_legacy_hit(self):
        report = ops_entry.call_check_context(self.conn, "998955601166")
        report["sip_hits"][0]["outbound_caller_id"] = ""
        report["pcap_events"] = [{
            "call_id": "wanted-call-id", "direction": "out", "method": "INVITE",
            "from_user": "49333444555",
        }]
        data = {"money_scale": db.MONEY_SCALE, "sip_hits": [], "cdr": [], "pcap_events": []}
        with mock.patch.object(bot, "_load_call_check", return_value=report):
            text, _ = bot._answer_for_text(data, "/check 998955601166")

        self.assertIn("A-номер поставщику: 49333444555", text)

    def test_telegram_check_reports_missing_number_without_unrelated_hit(self):
        report = {"sip_hits": [], "cdr": [], "pcap_events": []}
        data = {"sip_hits": [{"destination": "996502874006"}], "cdr": []}
        with mock.patch.object(bot, "_load_call_check", return_value=report):
            text, _ = bot._answer_for_text(data, "/check 998000000000")

        self.assertIn("не найдены", text)
        self.assertNotIn("996502874006", text)

    def test_telegram_check_skips_large_diagnostics_request(self):
        update = bot.TelegramUpdate(message={
            "chat": {"id": 123},
            "text": "/check 998955601166",
        })
        with (
            mock.patch.object(bot, "_chat_allowed", return_value=True),
            mock.patch.object(bot, "_load_diagnostics") as load_diagnostics,
            mock.patch.object(bot, "_load_call_check", return_value={
                "money_scale": db.MONEY_SCALE,
                "sip_hits": [],
                "cdr": [],
                "pcap_events": [],
            }),
            mock.patch.object(bot, "_send_response") as send_response,
        ):
            result = bot.telegram_webhook(update)

        self.assertEqual(result, {"ok": True})
        load_diagnostics.assert_not_called()
        self.assertIn("не найдены", send_response.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
