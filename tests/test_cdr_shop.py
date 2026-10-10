import unittest
import tempfile
from unittest import mock
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import telegram_cdr_shop_patch as cdr_shop
import telegram_standalone as bot
import cdr_shop_patch as cdr_api
import db


class FakeBot:
    DocumentResponse = bot.DocumentResponse

    @staticmethod
    def _client_name(client):
        return client["name"]


class CdrShopFileTests(unittest.TestCase):
    def test_decimal_minute_threshold_keeps_connected_calls(self):
        self.assertEqual(cdr_shop._parse_duration_seconds("0.01"), 0)

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

    def test_builds_country_document_with_numbers_only(self):
        report = {
            "country_name": "Poland",
            "cdr": [
                {"started_at": "2026-10-10 08:00:00", "dial_destination": "48500111222"},
                {"started_at": "2026-10-10 08:01:00", "dial_destination": "48500333444"},
            ],
        }
        result = cdr_shop._format_country_duration_report(FakeBot(), "pl", report)
        self.assertIsInstance(result, bot.DocumentResponse)
        self.assertEqual(result.file_name, "CDR_PL_10-10.txt")
        self.assertEqual(result.content.decode("utf-8"), "48500111222\n48500333444\n")
        self.assertEqual(result.caption, "Poland: 2 B-номеров")

    def test_iso_country_dictionary_has_requested_codes(self):
        self.assertEqual(cdr_api._iso_country_names()["PL"], "Poland")
        self.assertEqual(cdr_api._iso_country_names()["CZ"], "Czech Republic")

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


class CountryCdrApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "billing.db"
        db.init_db()
        conn = db.get_conn()
        first_client = conn.execute(
            "INSERT INTO clients (name, sip_ip) VALUES ('First', '198.51.100.1')"
        ).lastrowid
        second_client = conn.execute(
            "INSERT INTO clients (name, sip_ip) VALUES ('Second', '198.51.100.2')"
        ).lastrowid
        conn.executemany(
            """INSERT INTO cdr
                   (client_id, destination, client_tech_prefix, dial_destination,
                    billsec, sell_rate_cents, cost_rate_cents, charged_cents, margin_cents)
               VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0)""",
            [
                (first_client, "10348500111222", "103", "48500111222", 4),
                (second_client, "48500333444", "", "48500333444", 2),
                (first_client, "420702342319", "", "420702342319", 8),
                (first_client, "48500999888", "", "48500999888", 0),
            ],
        )
        conn.commit()
        conn.close()

        class Main:
            API_AUTH = []

        app = FastAPI()
        cdr_api.install(app, Main, db)
        self.client = TestClient(app)

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tmp.cleanup()

    def test_country_filter_collects_connected_calls_from_all_clients(self):
        response = self.client.get("/api/ops/cdr-duration-country/PL?min_billsec=0")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["country_code"], "PL")
        self.assertEqual(payload["country_name"], "Poland")
        self.assertEqual(
            [row["dial_destination"] for row in payload["cdr"]],
            ["48500333444", "48500111222"],
        )

    def test_unknown_country_code_is_rejected_cleanly(self):
        response = self.client.get("/api/ops/cdr-duration-country/XX?min_billsec=0")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
