import unittest
from unittest import mock

import telegram_cdr_shop_patch as cdr_shop
import telegram_standalone as bot


class FakeBot:
    DocumentResponse = bot.DocumentResponse

    @staticmethod
    def _client_name(client):
        return client["name"]


class CdrShopFileTests(unittest.TestCase):
    def test_prefers_clean_dial_destination(self):
        row = {
            "destination": "777420702342319",
            "client_tech_prefix": "777",
            "dial_destination": "420702342319",
            "provider_number": "999420702342319",
        }
        self.assertEqual(cdr_shop._cdr_number(row), "420702342319")

    def test_strips_client_prefix_when_dial_destination_is_missing(self):
        row = {"destination": "+777-420-702-342-319", "client_tech_prefix": "777"}
        self.assertEqual(cdr_shop._cdr_number(row), "420702342319")

    def test_builds_plain_text_document_with_numbers_only(self):
        report = {
            "cdr": [
                {
                    "started_at": "2026-10-02 12:49:30",
                    "destination": "777420702342319",
                    "client_tech_prefix": "777",
                    "dial_destination": "420702342319",
                    "result": "CALL_REJECTED",
                },
                {
                    "started_at": "2026-10-02 12:50:00",
                    "destination": "777420735958773",
                    "client_tech_prefix": "777",
                    "dial_destination": "420735958773",
                    "result": "NORMAL_CLEARING",
                },
            ]
        }
        result = cdr_shop._format_duration_report(FakeBot(), {"name": "UniVoIP Czechia"}, report)
        self.assertIsInstance(result, bot.DocumentResponse)
        self.assertEqual(result.file_name, "UniVoIP_Czechia_02-10.txt")
        self.assertEqual(result.content.decode("utf-8"), "420702342319\n420735958773\n")
        self.assertEqual(result.content_type, "text/plain; charset=utf-8")

    def test_document_response_is_uploaded_with_keyboard(self):
        document = bot.DocumentResponse(
            file_name="client_02-10.txt",
            content=b"420702342319\n",
            content_type="text/plain; charset=utf-8",
        )
        keyboard = {"inline_keyboard": [[{"text": "Back", "callback_data": "menu"}]]}
        with mock.patch.object(bot, "_send_document") as send_document:
            bot._send_response(123, document, keyboard)
        send_document.assert_called_once_with(
            123,
            "client_02-10.txt",
            b"420702342319\n",
            "",
            "text/plain; charset=utf-8",
            keyboard,
        )


if __name__ == "__main__":
    unittest.main()
