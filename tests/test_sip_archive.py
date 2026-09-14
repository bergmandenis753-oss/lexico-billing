import io
import json
import socket
import struct
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import sip_archive_store as store
import telegram_sip_archive as bot_archive
from freeswitch import telegram_sip_archive as collector


DAY = "2026-09-14"
LOCAL = "207.154.192.34"
B = "48506147819"
A = "12025550123"
SENT = "48732221920"


def packet(call="out-id", a=SENT, b=B, direction="out", at=DAY + "T10:00:00+00:00"):
    return {"at": at, "direction": direction, "src_ip": LOCAL if direction == "out" else "192.0.2.1",
            "src_port": 5060, "dst_ip": "192.0.2.2" if direction == "out" else LOCAL,
            "dst_port": 5060, "call_id": call, "cseq": "1 INVITE", "method": "INVITE",
            "b": b, "a": a, "pai": a, "rpid": "", "status": "", "reason": ""}


def channels():
    return [
        {"uuid": "in-uuid", "sip_id": "in-id", "call_uuid": "in-uuid", "origin_uuid": "", "peer_uuid": "out-uuid",
         "at": DAY + "T10:05:00+00:00", "started_at": DAY + "T09:59:59+00:00", "direction": "inbound", "ip": "192.0.2.1", "seconds": "123", "cause": "NORMAL_CLEARING"},
        {"uuid": "out-uuid", "sip_id": "out-id", "call_uuid": "in-uuid", "origin_uuid": "in-uuid", "peer_uuid": "in-uuid",
         "at": DAY + "T10:05:00+00:00", "direction": "outbound", "ip": "192.0.2.2", "seconds": "123", "cause": "NORMAL_CLEARING"},
    ]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = store.Archive(Path(self.tmp.name) / "archive.db")
        self.archive.add([packet(), packet("in-id", A, "105" + B, "in")], channels())

    def test_a_search_finds_actual_substituted_a(self):
        result = self.archive.query(A, DAY)
        self.assertEqual({e["call_id"] for e in result["events"]}, {"in-id", "out-id"})
        document = bot_archive.format_report(result, {"clients": [{"name": "Client", "ips": "192.0.2.1"}],
                                                    "termination_groups": [{"name": "Provider", "ips": "192.0.2.2"}]})
        self.assertIn("A входящий: " + A, document.text)
        self.assertIn("A отправленный (From): " + SENT, document.text)
        self.assertIn("2:03 (123 сек.)", document.text)
        self.assertIn("Начало: " + DAY + "T09:59:59", document.text)
        self.assertIn("Клиент: Client", document.text)
        self.assertNotIn("Исходящий A: не подтверждён", document.text)

    def test_b_search_and_technical_prefix(self):
        self.assertEqual(len(self.archive.query(B, DAY)["events"]), 2)
        self.assertEqual(len(self.archive.query(SENT, DAY)["events"]), 2)

    def test_no_time_based_guess(self):
        self.archive.add([packet("another-call", a="48111111111")])
        doc = bot_archive.format_report(self.archive.query(B, DAY), {})
        self.assertIn("связь с входящим каналом не сохранена", doc.text)
        self.assertIn("48111111111", doc.text)

    def test_failed_incoming_has_no_fabricated_outgoing(self):
        self.archive.add([packet("failed", a="12223334444", direction="in")])
        result = self.archive.query("12223334444", DAY)
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("Исходящий A: не подтверждён", bot_archive.format_report(result, {}).text)

    def test_complete_day_no_50_200_cap_and_restart_persistence(self):
        self.archive.add([packet("call-" + str(i), a="48123456789") for i in range(450)])
        fresh = store.Archive(self.archive.path)
        self.assertEqual(len(fresh.query("48123456789", DAY)["events"]), 450)
        self.assertEqual(len(fresh.query("48123456789", "2026-09-13")["events"]), 0)

    def test_retransmit_dedup_and_response_saved(self):
        self.archive.add([packet(at=DAY + "T10:00:02+00:00")])
        self.assertEqual(len(self.archive.query(B, DAY)["events"]), 2)
        response = {**packet(), "method": "", "b": "", "status": "503 Service Unavailable", "direction": "in", "src_ip": "192.0.2.2", "dst_ip": LOCAL}
        self.archive.add([response])
        self.assertIn("503 Service Unavailable", bot_archive.format_report(self.archive.query(A, DAY), {}).text)

    def test_prune_safe_and_validation(self):
        self.archive.prune(days=10000)
        for value in ("x", "123", "48%", "48506147819' OR 1=1"):
            with self.assertRaises(ValueError):
                self.archive.query(value, DAY)
        with self.assertRaises(ValueError):
            self.archive.query(A, "2026-99-99")

    def test_channel_update_preserves_identifiers_and_zero_duration(self):
        self.archive.add(channels=[{"uuid": "out-uuid", "sip_id": "", "at": DAY, "seconds": "0", "cause": "USER_BUSY"}])
        doc = bot_archive.format_report(self.archive.query(A, DAY), {})
        self.assertIn("0:00 (0 сек.)", doc.text)
        self.assertIn("USER_BUSY", doc.text)


class ParsingTests(unittest.TestCase):
    def payload(self):
        return (f"INVITE sip:{B}@192.0.2.2 SIP/2.0\r\nFrom: <sip:{SENT}@example.com>\r\n"
                f"P-Asserted-Identity: <sip:{SENT}@example.com>\r\nCall-ID: call-1\r\nCSeq: 1 INVITE\r\n"
                "Authorization: very-secret\r\nContent-Length: 0\r\n\r\n").encode()

    def test_headers_only_no_credentials(self):
        event = store.parse_sip(self.payload(), (LOCAL, 5060), ("192.0.2.2", 5060), {LOCAL}, DAY)
        self.assertEqual(event["a"], SENT)
        self.assertEqual(event["b"], B)
        self.assertNotIn("very-secret", json.dumps(event))
        self.assertIsNone(store.parse_sip(b"REGISTER sip:x SIP/2.0\r\nCSeq: 1 REGISTER\r\nCall-ID: x\r\n", (LOCAL, 5060), ("192.0.2.2", 5060), {LOCAL}, DAY))

    def test_pcap_binary_complete_last_packet(self):
        payload = self.payload()
        udp = struct.pack("!HHHH", 5060, 5060, len(payload) + 8, 0) + payload
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, socket.inet_aton(LOCAL), socket.inet_aton("192.0.2.2")) + udp
        frame = struct.pack("!HHIHBB8s", 0x0800, 0, 1, 1, 4, 6, b"\0" * 8) + ip
        pcap = struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 276)
        pcap += struct.pack("<IIII", 1789380000, 123456, len(frame), len(frame)) + frame
        at, raw, link = next(collector.pcap_records(io.BytesIO(pcap)))
        src, dst, content = collector.udp_packet(raw, link)
        self.assertEqual(content, payload)
        self.assertEqual(src, (LOCAL, 5060))
        self.assertIn("123456", at)

    def test_esl_content_length_and_channel_fields(self):
        event = {"Unique-ID": "b", "variable_originating_leg_uuid": "a", "Call-Direction": "outbound",
                 "variable_sip_call_id": "wire-id", "Caller-Channel-Created-Time": "1789380000000000"}
        body = json.dumps(event).encode()
        frame = b"Content-Type: text/event-json\nContent-Length: " + str(len(body)).encode() + b"\n\n" + body
        _, actual = collector.read_esl(io.BytesIO(frame))
        channel = store.channel_event(json.loads(actual))
        self.assertEqual(channel["origin_uuid"], "a")
        self.assertEqual(channel["sip_id"], "wire-id")
        self.assertIn("started_at", channel)

    def test_fragmented_invite_out_of_order(self):
        payload = self.payload() + b"x" * 2000
        udp = struct.pack("!HHHH", 5060, 5060, len(payload) + 8, 0) + payload
        frames = []
        for offset, data, more in ((0, udp[:1400], True), (1400, udp[1400:], False)):
            ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(data), 99, offset // 8 | (0x2000 if more else 0),
                             64, 17, 0, socket.inet_aton(LOCAL), socket.inet_aton("192.0.2.2")) + data
            frames.append(b"\0" * 12 + b"\x08\x00" + ip)
        fragments = collector.Fragments()
        self.assertIsNone(collector.udp_packet(frames[1], 1, fragments))
        _, _, actual = collector.udp_packet(frames[0], 1, fragments)
        self.assertEqual(actual, payload)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.bot = types.SimpleNamespace(_send_message=Mock(), _billing_key=lambda: "test-key", MAIN_MENU={})
        self.sender = self.bot._send_message
        self.app = FastAPI()
        bot_archive.install(self.app, self.bot)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + bot_archive.archive_token("test-key")}
        self.relay = self.bot._sip_archive_relay
        self.relay.last_poll = time.monotonic()

    def test_request_date_and_a(self):
        request = bot_archive.request_report(A + " " + DAY, {})
        self.assertEqual(request.day, DAY)
        self.assertEqual(request.number, A)

    def test_auth_required_and_result_delivered_once_to_requesting_chat(self):
        self.assertEqual(self.client.get("/internal/sip-archive/next").status_code, 403)
        self.relay.submit(123, bot_archive.request_report(A + " " + DAY, {}))
        job = self.client.get("/internal/sip-archive/next", headers=self.headers).json()["job"]
        path = "/internal/sip-archive/result/" + job["id"]
        result = {"number": A, "day": DAY, "events": [packet()], "channels": []}
        self.assertEqual(self.client.post(path, headers=self.headers, json=result).status_code, 200)
        self.client.post(path, headers=self.headers, json=result)
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0], 123)
        self.assertIn(SENT, self.sender.call_args.args[1].text)

    def test_unavailable_archive_never_claims_success(self):
        self.relay.last_poll = 0
        self.assertIn("не подключён", self.relay.submit(123, bot_archive.request_report(A, {})))


if __name__ == "__main__":
    unittest.main()
