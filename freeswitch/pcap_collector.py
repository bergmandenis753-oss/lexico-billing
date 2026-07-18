#!/usr/bin/env python3
"""Passive SIP packet collector for Lexico billing diagnostics.

The collector does not control FreeSWITCH. It only watches SIP packets with
tcpdump, parses the call ladder basics, and pushes compact events to billing.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


DEFAULT_API = "https://web-production-d5e1c.up.railway.app"
DEFAULT_KEY_FILE = "/etc/freeswitch/billing_api_key"
DEFAULT_LOCAL_IPS = "207.154.192.34,10.114.0.2"

API = os.getenv("BILLING_API", DEFAULT_API).rstrip("/")
KEY_FILE = os.getenv("BILLING_API_KEY_FILE", DEFAULT_KEY_FILE)
IFACE = os.getenv("LEXICO_PCAP_IFACE", "any")
LOCAL_IPS = {
    token.strip()
    for token in os.getenv("LEXICO_LOCAL_IPS", DEFAULT_LOCAL_IPS).replace(";", ",").split(",")
    if token.strip()
}
FILTER = os.getenv("LEXICO_PCAP_FILTER", "udp port 5060 or udp port 5080")
BATCH_SIZE = int(os.getenv("LEXICO_PCAP_BATCH_SIZE", "12"))
FLUSH_INTERVAL = float(os.getenv("LEXICO_PCAP_FLUSH_INTERVAL", "1.5"))
MAX_SUMMARY = int(os.getenv("LEXICO_PCAP_MAX_SUMMARY", "1600"))

PACKET_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"IP6?\s+(?P<src>\S+)\s+>\s+(?P<dst>\S+):\s+UDP",
)
SIP_START_RE = re.compile(
    r"(SIP/2\.0\s+(?P<code>\d{3})\s*(?P<text>[^\r\n]*)|"
    r"(?P<method>INVITE|ACK|BYE|CANCEL|OPTIONS|REGISTER|INFO|PRACK|UPDATE)\s+"
    r"(?P<uri>\S+)\s+SIP/2\.0)",
    re.IGNORECASE,
)


def read_key():
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def split_endpoint(value):
    value = value.strip().rstrip(":")
    host, sep, port = value.rpartition(".")
    if sep and port.isdigit():
        return host, port
    return value, ""


def header_value(payload, name):
    match = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", payload)
    return match.group(1).strip() if match else ""


def sip_user(header):
    match = re.search(r"sip:([^@>;]+)", header or "", re.IGNORECASE)
    return match.group(1) if match else ""


def packet_direction(src_ip, dst_ip):
    if src_ip in LOCAL_IPS:
        return "out"
    if dst_ip in LOCAL_IPS:
        return "in"
    return "unknown"


def summarize_payload(first_line, payload):
    keep = [first_line]
    for name in ("Call-ID", "CSeq", "From", "To", "Via", "Contact", "User-Agent", "Reason"):
        value = header_value(payload, name)
        if value:
            keep.append(f"{name}: {value}")
    summary = "\n".join(keep)
    return summary[:MAX_SUMMARY]


def parse_packet(meta, lines):
    payload = "\n".join(lines)
    match = SIP_START_RE.search(payload)
    if not match:
        return None

    sip_payload = payload[match.start():]
    first_line = match.group(0).strip()
    src_ip, src_port = split_endpoint(meta["src"])
    dst_ip, dst_port = split_endpoint(meta["dst"])
    call_id = header_value(sip_payload, "Call-ID") or header_value(sip_payload, "i")
    cseq = header_value(sip_payload, "CSeq")
    from_header = header_value(sip_payload, "From") or header_value(sip_payload, "f")
    to_header = header_value(sip_payload, "To") or header_value(sip_payload, "t")

    status_code = None
    status_text = ""
    method = ""
    request_uri = ""
    if match.group("code"):
        status_code = int(match.group("code"))
        status_text = (match.group("text") or "").strip()
    else:
        method = (match.group("method") or "").upper()
        request_uri = match.group("uri") or ""

    return {
        "observed_at": meta["ts"],
        "direction": packet_direction(src_ip, dst_ip),
        "src_ip": src_ip,
        "src_port": src_port,
        "dst_ip": dst_ip,
        "dst_port": dst_port,
        "method": method,
        "status_code": status_code,
        "status_text": status_text,
        "call_id": call_id,
        "cseq": cseq,
        "from_user": sip_user(from_header),
        "to_user": sip_user(to_header),
        "request_uri": request_uri,
        "user_agent": header_value(sip_payload, "User-Agent"),
        "reason": header_value(sip_payload, "Reason"),
        "raw_summary": summarize_payload(first_line, sip_payload),
    }


def post_events(events):
    if not events:
        return
    key = read_key()
    if not key:
        print("missing billing api key", file=sys.stderr, flush=True)
        return
    body = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        API + "/api/pcap-events",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"billing api http {exc.code}: {detail[:300]}", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"billing api error: {exc}", file=sys.stderr, flush=True)


def run():
    cmd = ["tcpdump", "-i", IFACE, "-nn", "-tttt", "-l", "-A", "-s", "0", FILTER]
    print("starting: " + " ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    current_meta = None
    current_lines = []
    batch = []
    last_flush = time.monotonic()

    def flush_packet():
        nonlocal current_meta, current_lines
        if current_meta is not None:
            event = parse_packet(current_meta, current_lines)
            if event and event.get("call_id"):
                batch.append(event)
        current_meta = None
        current_lines = []

    def flush_batch(force=False):
        nonlocal batch, last_flush
        now = time.monotonic()
        if batch and (force or len(batch) >= BATCH_SIZE or now - last_flush >= FLUSH_INTERVAL):
            post_events(batch)
            batch = []
            last_flush = now

    assert proc.stdout is not None
    while not stopping:
        line = proc.stdout.readline()
        if line == "" and proc.poll() is not None:
            break
        if not line:
            flush_batch()
            continue
        line = line.rstrip("\n")
        match = PACKET_RE.match(line)
        if match:
            flush_packet()
            current_meta = match.groupdict()
            current_lines = []
            flush_batch()
        elif current_meta is not None:
            current_lines.append(line)

    flush_packet()
    flush_batch(force=True)
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(run())
