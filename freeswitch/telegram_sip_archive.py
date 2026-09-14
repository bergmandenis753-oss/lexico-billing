#!/usr/bin/env python3
"""Passive archive worker. Never executes FreeSWITCH api/bgapi/sendmsg commands."""

import hashlib
import hmac
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from sip_archive_store import Archive, channel_event, parse_sip, utcnow


BOT_URL = "https://bot2-production-3c97.up.railway.app"
KEY_FILE = "/etc/freeswitch/billing_api_key"
ESL_CONFIG = "/etc/freeswitch/autoload_configs/event_socket.conf.xml"
LOCAL_IPS = {"207.154.192.34", "10.114.0.2", "10.19.0.5", "127.0.0.1"}
STOP = threading.Event()


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("Stream closed")
        chunks.extend(chunk)
    return bytes(chunks)


class Fragments:
    def __init__(self):
        self.pending = {}

    def join(self, ip):
        now = time.monotonic()
        for key in list(self.pending):
            if now - self.pending[key][0] > 5:
                del self.pending[key]
        ihl = (ip[0] & 15) * 4
        field = struct.unpack("!H", ip[6:8])[0]
        offset, more = (field & 0x1FFF) * 8, bool(field & 0x2000)
        length = struct.unpack("!H", ip[2:4])[0]
        key = (ip[12:20], ip[4:6], ip[9])
        if len(self.pending) >= 256 and key not in self.pending:
            raise ValueError("Fragment cache full")
        created, chunks, end, header = self.pending.get(key, (now, {}, None, None))
        if length < ihl or len(ip) < length or offset + length - ihl > 65535:
            raise ValueError("Invalid fragment length")
        for pos, chunk in chunks.items():
            if pos != offset and max(pos, offset) < min(pos + len(chunk), offset + length - ihl):
                self.pending.pop(key, None)
                raise ValueError("Overlapping fragments")
        chunks[offset] = ip[ihl:length]
        if not more:
            end = offset + length - ihl
        if offset == 0:
            header = ip[:ihl]
        if len(chunks) > 64:
            self.pending.pop(key, None)
            raise ValueError("Too many fragments")
        self.pending[key] = (created, chunks, end, header)
        cursor, parts = 0, []
        for pos, chunk in sorted(chunks.items()):
            if pos != cursor:
                return None
            parts.append(chunk)
            cursor += len(chunk)
        if header and cursor == end:
            del self.pending[key]
            return header[:6] + b"\0\0" + header[8:] + b"".join(parts)
        return None


def udp_packet(frame, linktype, fragments=None):
    if linktype == 1:
        protocol = struct.unpack("!H", frame[12:14])[0]
        offset = 14
        if protocol in (0x8100, 0x88A8):
            protocol = struct.unpack("!H", frame[16:18])[0]
            offset = 18
    elif linktype == 113:
        protocol, offset = struct.unpack("!H", frame[14:16])[0], 16
    elif linktype == 276:
        protocol, offset = struct.unpack("!H", frame[:2])[0], 20
    else:
        raise ValueError("Unsupported pcap link type")
    ip = frame[offset:]
    if protocol != 0x0800 or len(ip) < 28 or ip[9] != 17:
        return None
    ihl = (ip[0] & 15) * 4
    if ihl < 20:
        raise ValueError("Invalid IP header")
    if struct.unpack("!H", ip[6:8])[0] & 0x3FFF:
        if fragments is not None:
            ip = fragments.join(ip)
            if ip is None:
                return None
            ihl = (ip[0] & 15) * 4
        else:
            raise ValueError("Fragmented IP packet")
    if len(ip) < ihl + 8:
        raise ValueError("Fragmented or invalid IP packet")
    sport, dport, size = struct.unpack("!HHH", ip[ihl:ihl + 6])
    if sport not in (5060, 5080) and dport not in (5060, 5080):
        return None
    if len(ip) < ihl + size or size < 8:
        raise ValueError("Truncated UDP packet")
    return ((socket.inet_ntoa(ip[12:16]), sport), (socket.inet_ntoa(ip[16:20]), dport), ip[ihl + 8:ihl + size])


def pcap_records(stream):
    header = read_exact(stream, 24)
    formats = {b"\xd4\xc3\xb2\xa1": ("<", 1000000), b"\xa1\xb2\xc3\xd4": (">", 1000000),
               b"\x4d\x3c\xb2\xa1": ("<", 1000000000), b"\xa1\xb2\x3c\x4d": (">", 1000000000)}
    endian, scale = formats[header[:4]]
    linktype = struct.unpack(endian + "I", header[20:24])[0]
    if linktype not in (1, 113, 276):
        raise ValueError("Unsupported link type")
    while not STOP.is_set():
        seconds, fraction, size, _ = struct.unpack(endian + "IIII", read_exact(stream, 16))
        if size > 262144:
            raise ValueError("Invalid pcap record size")
        frame = read_exact(stream, size)
        at = datetime.fromtimestamp(seconds + fraction / scale, timezone.utc).isoformat(timespec="microseconds")
        yield at, frame, linktype


def capture(archive, inbox):
    fragments = Fragments()
    while not STOP.is_set():
        proc = None
        try:
            proc = subprocess.Popen(["/usr/bin/tcpdump", "-i", "any", "-nn", "-U", "-s", "0", "-w", "-",
                                     "ip proto 17 and ((udp port 5060 or udp port 5080) or (ip[6:2] & 0x1fff != 0))"], stdout=subprocess.PIPE)
            archive.set_meta("capture_status", "running")
            for at, frame, linktype in pcap_records(proc.stdout):
                try:
                    packet = udp_packet(frame, linktype, fragments)
                    if packet:
                        src, dst, payload = packet
                        event = parse_sip(payload, src, dst, LOCAL_IPS, at)
                        if event and event["direction"] != "unknown":
                            inbox.put_nowait(("sip", event))
                except (ValueError, struct.error, queue.Full):
                    archive.set_meta("last_gap", utcnow() + " packet skipped / queue full")
        except Exception as exc:
            archive.set_meta("capture_status", "reconnecting")
            archive.set_meta("last_gap", utcnow() + " capture interrupted")
            print("capture reconnect:", type(exc).__name__, flush=True)
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        STOP.wait(3)


def read_esl(stream):
    headers = {}
    while True:
        line = stream.readline(65537)
        if not line:
            raise EOFError()
        if len(line) > 65536:
            raise ValueError("ESL header too long")
        line = line.decode("utf-8", "replace").strip()
        if not line:
            break
        key, value = line.split(":", 1)
        headers[key] = value.strip()
    size = int(headers.get("Content-Length", 0))
    if size < 0 or size > 2_000_000:
        raise ValueError("ESL event too large")
    return headers, read_exact(stream, size)


def events(archive, inbox):
    while not STOP.is_set():
        try:
            config = ET.parse(ESL_CONFIG)
            password = next(p.attrib["value"] for p in config.iter("param") if p.attrib.get("name") == "password")
            with socket.create_connection(("127.0.0.1", 8021), timeout=10) as sock:
                sock.settimeout(None)
                stream = sock.makefile("rb")
                read_esl(stream)
                sock.sendall(("auth " + password + "\n\n").encode())
                headers, _ = read_esl(stream)
                if not headers.get("Reply-Text", "").startswith("+OK"):
                    raise ValueError("ESL authentication failed")
                # Subscriptions only. No call-control or configuration commands.
                sock.sendall(b"event json CHANNEL_CREATE CHANNEL_BRIDGE CHANNEL_HANGUP_COMPLETE HEARTBEAT\n\n")
                archive.set_meta("events_status", "running")
                while not STOP.is_set():
                    headers, body = read_esl(stream)
                    if headers.get("Content-Type") == "text/event-json":
                        item = channel_event(json.loads(body))
                        if item:
                            try:
                                inbox.put_nowait(("channel", item))
                            except queue.Full:
                                archive.set_meta("last_gap", utcnow() + " channel queue full")
        except Exception as exc:
            archive.set_meta("events_status", "reconnecting")
            archive.set_meta("last_gap", utcnow() + " channel subscription interrupted")
            print("event reconnect:", type(exc).__name__, flush=True)
        STOP.wait(3)


def writer(archive, inbox):
    last_prune = 0
    while not STOP.is_set():
        batch = []
        try:
            batch.append(inbox.get(timeout=1))
        except queue.Empty:
            pass
        while len(batch) < 250:
            try:
                batch.append(inbox.get_nowait())
            except queue.Empty:
                break
        if batch:
            archive.add([v for k, v in batch if k == "sip"], [v for k, v in batch if k == "channel"])
        if time.monotonic() - last_prune > 3600:
            archive.prune()
            last_prune = time.monotonic()
        if Path(archive.path).stat().st_size > 2_000_000_000:
            archive.set_meta("last_gap", utcnow() + " storage safety limit reached")
            raise RuntimeError("Archive storage safety limit reached")


def api(path, body=None):
    key = Path(KEY_FILE).read_text().strip()
    token = hmac.new(key.encode(), b"lexico-telegram-sip-archive-v1", hashlib.sha256).hexdigest()
    request = urllib.request.Request(BOT_URL + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read(26_000_000))


def relay(archive):
    pending = None
    while not STOP.is_set():
        try:
            if pending:
                job_id, result = pending
                api("/internal/sip-archive/result/" + job_id, result)
                pending = None
            data = api("/internal/sip-archive/next")
            job = data.get("job")
            if job:
                if not isinstance(job.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", job["id"]):
                    raise ValueError("Invalid job ID")
                try:
                    result = archive.query(job["number"], job["day"])
                except Exception:
                    result = {"number": job.get("number"), "day": job.get("day"), "error": "Query failed"}
                pending = (job["id"], result)
            archive.set_meta("relay_status", "connected")
        except Exception as exc:
            archive.set_meta("relay_status", "reconnecting")
            print("relay reconnect:", type(exc).__name__, flush=True)
            STOP.wait(5)


def main():
    os.umask(0o077)
    archive = Archive(os.getenv("SIP_ARCHIVE_DB", "/var/lib/lexico-telegram-archive/archive.sqlite3"))
    archive.set_meta("last_gap", utcnow() + " collector started/restarted")
    inbox = queue.Queue(maxsize=10000)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: STOP.set())
    workers = [threading.Thread(target=fn, args=args, daemon=True) for fn, args in
               ((capture, (archive, inbox)), (events, (archive, inbox)), (writer, (archive, inbox)), (relay, (archive,)))]
    for worker in workers:
        worker.start()
    while not STOP.wait(2):
        if not all(worker.is_alive() for worker in workers):
            raise RuntimeError("Archive worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
