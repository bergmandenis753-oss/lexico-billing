#!/usr/bin/env python3
"""Collect low-overhead CPU, active-call and post-call RTP quality metrics."""

import json
import math
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib.parse import unquote
from urllib.request import Request, urlopen


API_BASE = os.getenv("BILLING_API", "https://web-production-d5e1c.up.railway.app").rstrip("/")
API_KEY_FILE = Path(os.getenv("BILLING_API_KEY_FILE", "/etc/freeswitch/billing_api_key"))
ESL_HOST = os.getenv("FREESWITCH_ESL_HOST", "127.0.0.1")
ESL_PORT = int(os.getenv("FREESWITCH_ESL_PORT", "8021"))
ESL_PASSWORD = os.getenv("FREESWITCH_ESL_PASSWORD", "ClueCon")
INTERVAL_SECONDS = max(10, int(os.getenv("SYSTEM_MONITOR_INTERVAL", "15")))
SOURCE = os.getenv("SYSTEM_MONITOR_SOURCE", socket.gethostname())[:64]


class QualityAccumulator:
    def __init__(self):
        self.lock = threading.Lock()
        self.loss_weighted = 0.0
        self.jitter_weighted = 0.0
        self.packets = 0
        self.legs = 0

    def add_event(self, values):
        packets = _integer(values.get("variable_rtp_audio_in_packet_count"))
        if packets <= 0:
            return
        loss_rate = _number(values.get("variable_rtp_audio_in_jitter_loss_rate"))
        max_variance = _number(values.get("variable_rtp_audio_in_jitter_max_variance"))
        loss_percent = max(0.0, min(100.0, loss_rate * 100.0))
        jitter_ms = math.sqrt(max(0.0, max_variance))
        with self.lock:
            self.loss_weighted += loss_percent * packets
            self.jitter_weighted += jitter_ms * packets
            self.packets += packets
            self.legs += 1

    def take(self):
        with self.lock:
            if self.packets:
                payload = {
                    "packet_loss_percent": self.loss_weighted / self.packets,
                    "jitter_ms": self.jitter_weighted / self.packets,
                    "rtp_packets": self.packets,
                    "rtp_legs": self.legs,
                }
            else:
                payload = {
                    "packet_loss_percent": None,
                    "jitter_ms": None,
                    "rtp_packets": 0,
                    "rtp_legs": 0,
                }
            self.loss_weighted = 0.0
            self.jitter_weighted = 0.0
            self.packets = 0
            self.legs = 0
            return payload

    def restore(self, payload):
        packets = int(payload.get("rtp_packets") or 0)
        if packets <= 0:
            return
        with self.lock:
            self.loss_weighted += float(payload["packet_loss_percent"]) * packets
            self.jitter_weighted += float(payload["jitter_ms"]) * packets
            self.packets += packets
            self.legs += int(payload.get("rtp_legs") or 0)


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _cpu_ticks():
    fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    values.extend([0] * (10 - len(values)))
    total = sum(values)
    idle = values[3] + values[4]
    steal = values[7]
    return total, idle, steal


def cpu_percent(previous, current):
    total_delta = current[0] - previous[0]
    if total_delta <= 0:
        return 0.0, 0.0
    idle_delta = current[1] - previous[1]
    steal_delta = current[2] - previous[2]
    usage = (total_delta - idle_delta) * 100.0 / total_delta
    steal = steal_delta * 100.0 / total_delta
    return max(0.0, min(100.0, usage)), max(0.0, min(100.0, steal))


def active_calls():
    try:
        result = subprocess.run(
            ["fs_cli", "-x", "show channels as json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        rows = json.loads(result.stdout or "{}").get("rows") or []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return 0
    calls = {
        row.get("call_uuid") or row.get("uuid")
        for row in rows
        if str(row.get("callstate") or "").upper() == "ACTIVE"
    }
    calls.discard(None)
    return len(calls)


def _read_frame(sock, buffer):
    while b"\n\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("FreeSWITCH event socket closed")
        buffer += chunk
    raw_headers, buffer = buffer.split(b"\n\n", 1)
    headers = {}
    for line in raw_headers.decode("utf-8", errors="replace").splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            headers[key.lower()] = value
    length = int(headers.get("content-length") or 0)
    while len(buffer) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("FreeSWITCH event socket closed")
        buffer += chunk
    return headers, buffer[:length], buffer[length:]


def _event_values(body):
    values = {}
    for line in body.decode("utf-8", errors="replace").splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            values[key] = unquote(value)
    return values


def event_listener(accumulator):
    while True:
        try:
            with socket.create_connection((ESL_HOST, ESL_PORT), timeout=10) as sock:
                sock.settimeout(90)
                buffer = b""
                _, _, buffer = _read_frame(sock, buffer)
                sock.sendall(f"auth {ESL_PASSWORD}\n\n".encode())
                _, _, buffer = _read_frame(sock, buffer)
                sock.sendall(b"event plain CHANNEL_HANGUP_COMPLETE\n\n")
                _, _, buffer = _read_frame(sock, buffer)
                while True:
                    _, body, buffer = _read_frame(sock, buffer)
                    values = _event_values(body)
                    if values.get("Event-Name") == "CHANNEL_HANGUP_COMPLETE":
                        accumulator.add_event(values)
        except Exception as exc:
            print(f"event socket reconnect: {exc}", flush=True)
            time.sleep(3)


def _api_key():
    return API_KEY_FILE.read_text(encoding="utf-8").strip()


def post_sample(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        f"{API_BASE}/api/system-monitoring/ingest",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": _api_key(),
            "User-Agent": "lexico-system-monitor/1.0",
        },
    )
    with urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError(f"monitor API returned HTTP {response.status}")


def main():
    accumulator = QualityAccumulator()
    threading.Thread(target=event_listener, args=(accumulator,), daemon=True).start()
    previous_cpu = _cpu_ticks()
    time.sleep(1)
    while True:
        started = time.monotonic()
        current_cpu = _cpu_ticks()
        usage, steal = cpu_percent(previous_cpu, current_cpu)
        previous_cpu = current_cpu
        quality = accumulator.take()
        payload = {
            "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": SOURCE,
            "cpu_percent": usage,
            "cpu_steal_percent": steal,
            "active_calls": active_calls(),
            **quality,
        }
        try:
            post_sample(payload)
            print(
                f"sent cpu={usage:.1f}% calls={payload['active_calls']} "
                f"rtp_legs={quality['rtp_legs']}",
                flush=True,
            )
        except Exception as exc:
            accumulator.restore(quality)
            print(f"monitor API error: {exc}", flush=True)
        time.sleep(max(1, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
