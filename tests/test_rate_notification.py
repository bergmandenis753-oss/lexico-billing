import io
import unittest
import zipfile
from decimal import Decimal
from unittest import mock
from xml.etree import ElementTree

import rate_notification


NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class RateNotificationTests(unittest.TestCase):
    def test_parses_example(self):
        notice = rate_notification.parse_rate_command("/rate Poland 48 0.09 1/1 103")
        self.assertEqual(notice.destination, "Poland")
        self.assertEqual(notice.code, "48")
        self.assertEqual(notice.rate, Decimal("0.09"))
        self.assertEqual(notice.tarification, "1/1")
        self.assertEqual(notice.tech_prefix, "103")

    def test_destination_can_contain_spaces(self):
        notice = rate_notification.parse_rate_command("/rate United Kingdom 44 0,12 60/60 105")
        self.assertEqual(notice.destination, "United Kingdom")
        self.assertEqual(notice.rate_text, "0.12")

    def test_invalid_command_returns_help(self):
        with self.assertRaisesRegex(ValueError, "Пример"):
            rate_notification.parse_rate_command("/rate Poland")

    def test_builds_valid_workbook_with_expected_cells(self):
        notice = rate_notification.parse_rate_command("/rate Poland 48 0.09 1/1 103")
        content = rate_notification.build_rate_notification_xlsx(notice)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            sheet = workbook.find("x:sheets/x:sheet", NS)
            self.assertEqual(sheet.attrib["name"], "Rate Notification")
            xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            for value in ("Poland", "48", "0.09", "1/1", "103", "New Rate"):
                self.assertIn(value, xml)
        self.assertEqual(
            rate_notification.rate_notification_filename(notice),
            "RN_Poland_Rate_103_0.09.xlsx",
        )

    def test_bot_sends_generated_document(self):
        import telegram_standalone

        send_document = mock.Mock()
        patcher = mock.patch.object(telegram_standalone, "_send_document", send_document)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(
            telegram_standalone._send_rate_notification(
                123,
                "/rate Poland 48 0.09 1/1 103",
            )
        )
        args = send_document.call_args.args
        self.assertEqual(args[0], 123)
        self.assertEqual(args[1], "RN_Poland_Rate_103_0.09.xlsx")
        self.assertTrue(args[2].startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
