import io
import json
import os
import types
import unittest
import urllib.error
from email.parser import BytesParser
from email.policy import default
from unittest.mock import Mock, patch

import telegram_cdr_check as check
import telegram_cdr_shop_patch as shop
import telegram_report_files as files


def make_bot():
    client = {"id": 17, "name": "Metavoip"}
    return types.SimpleNamespace(
        MAIN_MENU={"inline_keyboard": []},
        _client_keyboard=lambda cid: {"inline_keyboard": [[{"text": "Client", "callback_data": f"client:{cid}"}]]},
        _answer_for_callback=lambda data, text: ("base callback", {}),
        _answer_for_text=lambda data, text: ("base text", {}),
        _send_message=Mock(),
        _token=lambda: "test-token",
        _client_by_id=lambda data, cid: client if str(cid) == "17" else None,
        _client_name=lambda row: row.get("name", "unknown"),
        _money=lambda value, scale, currency: f"{int(value or 0) / scale:.4f} {currency}",
        _button=lambda text, data: {"text": text, "callback_data": data},
        _keyboard=lambda rows: {"inline_keyboard": rows},
        _billing_base_url=lambda: "https://billing.test",
        _billing_headers=lambda: {"Authorization": "Bearer test-key"},
        _get_json=Mock(),
    )


def report(count):
    return {"limit": 200, "min_billsec": 310, "money_scale": 10000,
            "cdr": [{"id": i, "started_at": "2026-09-08 12:00:00", "billsec": 311 + i,
                     "provider_number": f"48{i:09d}", "charged_cents": 1234, "result": "Normal"}
                    for i in range(count)]}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.original_send = self.bot._send_message
        shop.install(None, self.bot)
        check.install(self.bot)

    def test_command_returns_complete_file(self):
        self.bot._get_json.return_value = report(200)
        document, keyboard = self.bot._answer_for_text({}, "/cdrshop 17 05:10")
        self.assertIsInstance(document, files.TextDocument)
        self.assertGreater(len(document.text), 3900)
        self.assertIn("48000000199", document.text)
        self.assertEqual(document.text.count("статус: Normal"), 200)
        self.assertIn("лимит API", document.caption)
        self.assertNotIn("обрезал", document.text)
        url = self.bot._get_json.call_args.args[0]
        self.assertIn("limit=200", url)
        self.assertIn("min_billsec=310", url)
        self.assertTrue(keyboard["inline_keyboard"])

    def test_pending_flow_also_returns_file_and_isolated_chats(self):
        self.bot._get_json.return_value = report(2)
        self.bot._cdr_shop_set_pending(10, "client_cdr_shop:17")
        self.assertIsNone(self.bot._cdr_shop_answer_pending({}, 11, "5"))
        document, _ = self.bot._cdr_shop_answer_pending({}, 10, "5")
        self.assertIsInstance(document, files.TextDocument)
        self.assertIn("min_billsec=300", self.bot._get_json.call_args.args[0])

    def test_empty_report_is_file(self):
        self.bot._get_json.return_value = report(0)
        document, _ = self.bot._answer_for_text({}, "/cdrshop@lexico 17 05:10")
        self.assertIn("Нет звонков", document.text)
        self.assertNotIn("лимит", document.caption)

    def test_unknown_client_and_bad_duration_do_not_query(self):
        for command in ("/cdrshop 0 5", "/cdrshop 17 nonsense"):
            answer, _ = self.bot._answer_for_text({}, command)
            self.assertIsInstance(answer, str)
        self.bot._get_json.assert_not_called()

    def test_normal_messages_and_idempotence(self):
        sender = self.bot._send_message
        shop.install(None, self.bot)
        files.install(self.bot)
        check.install(self.bot)
        self.assertIs(sender, self.bot._send_message)
        self.bot._send_message(10, "hello", {})
        self.original_send.assert_called_once_with(10, "hello", {})

    def test_multipart_upload_retains_every_byte_and_keyboard(self):
        document = files.TextDocument("cdrshop_17.txt", "Тест\n" * 10000, "CDR shop")
        keyboard = {"inline_keyboard": [[{"text": "Назад", "callback_data": "clients"}]]}
        with patch.object(files.urllib.request, "urlopen", return_value=io.BytesIO(b'{"ok":true}')) as send:
            files.send_document(self.bot, 10, document, keyboard)
        request = send.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/sendDocument"))
        self.assertEqual(request.get_method(), "POST")
        mime = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + request.get_header("Content-type") + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + request.data)
        parts = {part.get_param("name", header="Content-Disposition"): part for part in mime.iter_parts()}
        self.assertEqual(parts["document"].get_filename(), document.filename)
        self.assertEqual(parts["document"].get_payload(decode=True).decode("utf-8-sig"), document.text)
        self.assertEqual(json.loads(parts["reply_markup"].get_payload(decode=True)), keyboard)
        self.assertEqual(parts["chat_id"].get_payload(decode=True), b"10")

    def test_failed_upload_does_not_leak_token_or_fallback_to_truncated_report(self):
        error = urllib.error.HTTPError("https://api.telegram.org/botSECRET/sendDocument", 429, "error", {}, None)
        with patch.object(files.urllib.request, "urlopen", side_effect=error):
            self.bot._send_message(10, files.TextDocument("report.txt", "private data", "CDR"), {})
        text = self.original_send.call_args.args[1]
        self.assertIn("повтори", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("private data", text)

    def test_api_false_is_failure(self):
        with patch.object(files.urllib.request, "urlopen", return_value=io.BytesIO(b'{"ok":false}')):
            with self.assertRaises(RuntimeError):
                files.send_document(self.bot, 10, files.TextDocument("report.txt", "x", "x"))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        shop.install(None, self.bot)
        check.install(self.bot)
        self.cdr = {"id": 1, "call_uuid": "a-leg", "client_id": 17, "client_name": "Metavoip",
                    "destination": "10548506147819", "dial_destination": "48506147819",
                    "provider_number": "99948506147819", "route_ip": "192.0.2.10",
                    "clid": "12025550123", "terminator_name": "Test provider"}
        self.event = {"direction": "out", "method": "INVITE", "dst_ip": "192.0.2.10",
                      "dst_port": "5060", "request_uri": "sip:99948506147819@192.0.2.10",
                      "from_user": "48732221920", "call_id": "b-leg", "cseq": "1 INVITE",
                      "observed_at": "2026-09-08 12:00:00",
                      "raw_summary": "From: <sip:48732221920@192.0.2.1>\r\nP-Asserted-Identity: <sip:48732221920@192.0.2.1>"}
        self.data = {"cdr": [self.cdr], "pcap_events": [self.event]}

    def test_actual_wire_identity_not_incoming_clid(self):
        text, _ = self.bot._answer_for_text(self.data, "/check +48 506 147 819")
        self.assertIn("A (исходящий From): 48732221920", text)
        self.assertIn("B (отправленный поставщику): 99948506147819", text)
        self.assertIn("Metavoip", text)
        self.assertNotIn("12025550123", text)
        self.assertIn("Связь с конкретным CDR/клиентом не подтверждена", text)

    def test_exact_call_id_links_only_correct_record(self):
        self.cdr["outbound_sip_call_id"] = "b-leg"
        text = check._report(self.bot, self.data, "48506147819")
        self.assertNotIn("A отправленный: не подтверждён", text)
        self.assertNotIn("Связь с конкретным", text)

    def test_no_wire_data_never_uses_pool_or_incoming_a(self):
        self.data["pcap_events"] = []
        self.cdr["caller_id_pool"] = "48732221920"
        text = check._report(self.bot, self.data, "48506147819")
        self.assertIn("A отправленный: не подтверждён", text)
        self.assertNotIn("12025550123", text)
        self.assertNotIn("48732221920", text)

    def test_incoming_invite_and_outgoing_response_not_evidence(self):
        for field, value in (("direction", "in"), ("method", "ACK")):
            event = {**self.event, field: value}
            self.assertEqual(check._wire_events({"pcap_events": [event]}, "48506147819", [self.cdr]), [])

    def test_prefix_requires_provider_match(self):
        event = {**self.event, "dst_ip": "192.0.2.99"}
        self.assertEqual(check._wire_events({"pcap_events": [event]}, "48506147819", [self.cdr]), [])

    def test_retransmissions_deduplicated(self):
        self.data["pcap_events"].append(dict(self.event))
        self.assertEqual(len(check._wire_events(self.data, "48506147819", [self.cdr])), 1)

    def test_no_results_is_not_claim_of_no_calls(self):
        text = check._report(self.bot, {}, "48506147819")
        self.assertIn("Это не означает, что звонка не было", text)

    def test_check_prompt_next_message_and_cancel(self):
        answer = self.bot._cdr_shop_answer_pending(self.data, 10, "/check")
        self.assertIn("Пришли B-номер", answer[0])
        self.assertIsNone(self.bot._cdr_shop_answer_pending(self.data, 11, "48506147819"))
        answer = self.bot._cdr_shop_answer_pending(self.data, 10, "48506147819")
        self.assertIn("48732221920", answer[0])
        self.bot._cdr_shop_answer_pending(self.data, 10, "/check")
        self.bot._cdr_shop_set_pending(10, "menu")
        self.assertIsNone(self.bot._cdr_shop_answer_pending(self.data, 10, "48506147819"))

    def test_check_cancels_duration_prompt(self):
        self.bot._cdr_shop_set_pending(10, "client_cdr_shop:17")
        self.bot._cdr_shop_answer_pending(self.data, 10, "/check")
        self.bot._cdr_shop_answer_pending(self.data, 10, "48506147819")
        self.assertIsNone(self.bot._cdr_shop_answer_pending(self.data, 10, "5"))

    def test_long_check_becomes_file(self):
        self.data["cdr"] = [{**self.cdr, "id": i} for i in range(50)]
        document = check._report(self.bot, self.data, "48506147819")
        self.assertIsInstance(document, files.TextDocument)
        self.assertIn("CDR #49", document.text)

    def test_invalid_number_does_not_search(self):
        for number in ("", "x", "123", "48%", "48506147819; DROP TABLE cdr", "４８５０６１４７８１９"):
            with self.assertRaises(ValueError):
                check._parse_number(number)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import telegram_cdr_shop_entry
        import telegram_standalone
        self.bot = telegram_standalone
        self.client = TestClient(telegram_cdr_shop_entry.app)
        self.addCleanup(self.client.close)
        env = patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": "123", "TELEGRAM_ALLOW_ALL": "0",
                                    "TELEGRAM_WEBHOOK_SECRET": "test-secret"})
        env.start()
        self.addCleanup(env.stop)
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        self.data = {"clients": [{"id": 17, "name": "Metavoip"}], "cdr": [], "pcap_events": []}

    def post(self, text, chat=123, headers=None):
        return self.client.post("/telegram/webhook", headers=self.headers if headers is None else headers,
                                json={"message": {"chat": {"id": chat}, "text": text}})

    def test_real_webhook_uploads_document(self):
        with patch.object(self.bot, "_load_diagnostics", return_value=self.data), \
             patch.object(shop, "_load_client_cdr_duration", return_value=report(200)), \
             patch.object(files, "send_document") as send:
            response = self.post("/cdrshop 17 05:10")
        self.assertEqual(response.status_code, 200)
        self.assertIn("48000000199", send.call_args.args[2].text)
        self.assertEqual(send.call_args.args[1], 123)

    def test_real_webhook_check_prompt(self):
        with patch.object(self.bot, "_load_diagnostics", return_value=self.data), \
             patch.object(self.bot, "_send_message") as send:
            self.post("/check")
            response = self.post("48506147819")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Проверка B: 48506147819", send.call_args.args[1])

    def test_unauthorized_chat_cannot_get_cdr_or_check(self):
        with patch.object(self.bot, "_load_diagnostics") as load, \
             patch.object(files, "send_document") as send:
            self.post("/cdrshop 17 5", chat=456)
            self.post("/check 48506147819", chat=456)
        load.assert_not_called()
        send.assert_not_called()

    def test_bad_webhook_secret_rejected_before_reading_data(self):
        with patch.object(self.bot, "_load_diagnostics") as load:
            response = self.post("/cdrshop 17 5", headers={})
        self.assertEqual(response.status_code, 403)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
