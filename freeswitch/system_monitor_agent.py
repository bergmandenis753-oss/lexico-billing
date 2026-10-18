#!/usr/bin/env python3
"""Collect low-overhead CPU, active-call and post-call RTP quality metrics."""

import json
import http.client
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen


API_BASE = os.getenv("BILLING_API", "https://web-production-d5e1c.up.railway.app").rstrip("/")
API_KEY_FILE = Path(os.getenv("BILLING_API_KEY_FILE", "/etc/freeswitch/billing_api_key"))
VOICE_NODE_FILE = Path(os.getenv("VOICE_NODE_FILE", "/etc/freeswitch/voice_node"))
ESL_HOST = os.getenv("FREESWITCH_ESL_HOST", "127.0.0.1")
ESL_PORT = int(os.getenv("FREESWITCH_ESL_PORT", "8021"))
ESL_PASSWORD = os.getenv("FREESWITCH_ESL_PASSWORD", "ClueCon")
INTERVAL_SECONDS = max(10, int(os.getenv("SYSTEM_MONITOR_INTERVAL", "15")))
SOURCE = os.getenv("SYSTEM_MONITOR_SOURCE", socket.gethostname())[:64]
VALID_VOICE_NODES = {"primary", "secondary"}
INCIDENT_DIR = Path(os.getenv("SYSTEM_INCIDENT_DIR", "/var/log/lexico-incidents"))
LOSS_THRESHOLD = float(os.getenv("SYSTEM_INCIDENT_LOSS_PERCENT", "3"))
JITTER_THRESHOLD_MS = float(os.getenv("SYSTEM_INCIDENT_JITTER_MS", "40"))
CPU_THRESHOLD = float(os.getenv("SYSTEM_INCIDENT_CPU_PERCENT", "90"))
CPU_STEAL_THRESHOLD = float(os.getenv("SYSTEM_INCIDENT_STEAL_PERCENT", "10"))
INCIDENT_DURATION_SECONDS = max(30, int(os.getenv("SYSTEM_INCIDENT_DURATION_SECONDS", "90")))
INCIDENT_MAX_BYTES = max(1, int(os.getenv("SYSTEM_INCIDENT_MAX_MB", "8"))) * 1024 * 1024
INCIDENT_COOLDOWN_SECONDS = max(60, int(os.getenv("SYSTEM_INCIDENT_COOLDOWN_SECONDS", "600")))
INCIDENT_LOCAL_FILES = max(1, int(os.getenv("SYSTEM_INCIDENT_LOCAL_FILES", "3")))
INCIDENT_BPF_FILTER = os.getenv(
    "SYSTEM_INCIDENT_BPF_FILTER",
    "udp and (port 5060 or port 5080 or portrange 16384-32768)",
)
AUTOMATIC_INCIDENTS_ENABLED = os.getenv(
    "SYSTEM_INCIDENT_AUTOMATIC_ENABLED", "0"
).strip().lower() in {"1", "true", "yes"}
MANUAL_CAPTURE_FILE_BYTES = max(
    1, int(os.getenv("SYSTEM_MANUAL_CAPTURE_FILE_MB", "6"))
) * 1024 * 1024


def incident_reasons(payload):
    reasons = []
    loss = payload.get("packet_loss_percent")
    jitter = payload.get("jitter_ms")
    has_rtp = int(payload.get("rtp_packets") or 0) > 0
    if has_rtp and loss is not None and float(loss) >= LOSS_THRESHOLD:
        reasons.append(f"RTP packet loss {float(loss):.2f}%")
    if has_rtp and jitter is not None and float(jitter) >= JITTER_THRESHOLD_MS:
        reasons.append(f"RTP jitter {float(jitter):.1f} ms")
    if float(payload.get("cpu_percent") or 0) >= CPU_THRESHOLD:
        reasons.append(f"CPU {float(payload['cpu_percent']):.1f}%")
    if float(payload.get("cpu_steal_percent") or 0) >= CPU_STEAL_THRESHOLD:
        reasons.append(f"CPU steal {float(payload['cpu_steal_percent']):.1f}%")
    return reasons


def suspected_cause(payload, reasons=None):
    reasons = reasons or incident_reasons(payload)
    loss = float(payload.get("packet_loss_percent") or 0)
    jitter = float(payload.get("jitter_ms") or 0)
    cpu = float(payload.get("cpu_percent") or 0)
    steal = float(payload.get("cpu_steal_percent") or 0)
    if steal >= CPU_STEAL_THRESHOLD:
        return "Вероятна перегрузка физического хоста: высокий CPU steal."
    if cpu >= CPU_THRESHOLD:
        return "Вероятна локальная перегрузка голосового сервера: высокий CPU."
    if loss >= LOSS_THRESHOLD and jitter >= JITTER_THRESHOLD_MS:
        return "Вероятны сетевая перегрузка или очереди: одновременно выросли RTP loss и jitter."
    if loss >= LOSS_THRESHOLD:
        return "Потери вероятнее находятся в сетевом пути, у клиента или поставщика; CPU сервера не перегружен."
    if jitter >= JITTER_THRESHOLD_MS:
        return "Нестабильны интервалы прихода RTP-пакетов; вероятна проблема сетевого пути."
    return "Причина пока не определена; сохранена диагностика для разбора."


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


def configured_voice_node(path=None):
    configured = os.getenv("VOICE_NODE", "").strip().lower()
    node_file = Path(path) if path is not None else VOICE_NODE_FILE
    if not configured:
        try:
            configured = node_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            configured = "primary"
    return configured if configured in VALID_VOICE_NODES else "primary"


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


def _request_json(path, payload, method="POST", timeout=15):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        f"{API_BASE}{path}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": _api_key(),
            "User-Agent": "lexico-system-monitor/1.0",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        if response.status >= 300:
            raise RuntimeError(f"monitor API returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8") or "{}")


def _get_json(path, query=None, timeout=15):
    suffix = f"?{urlencode(query)}" if query else ""
    request = Request(
        f"{API_BASE}{path}{suffix}",
        method="GET",
        headers={
            "X-API-Key": _api_key(),
            "User-Agent": "lexico-system-monitor/1.0",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        if response.status >= 300:
            raise RuntimeError(f"monitor API returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8") or "{}")


def _upload_bundle(incident_id, content):
    request = Request(
        f"{API_BASE}/api/system-monitoring/incidents/{incident_id}/bundle",
        data=content,
        method="PUT",
        headers={
            "Content-Type": "application/gzip",
            "X-API-Key": _api_key(),
            "User-Agent": "lexico-system-monitor/1.0",
        },
    )
    with urlopen(request, timeout=60) as response:
        if response.status >= 300:
            raise RuntimeError(f"bundle API returned HTTP {response.status}")


def _upload_bundle_file(incident_id, path):
    target = urlsplit(API_BASE)
    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(target.hostname, target.port, timeout=90)
    endpoint = f"{target.path.rstrip('/')}/api/system-monitoring/incidents/{incident_id}/bundle"
    size = path.stat().st_size
    try:
        connection.putrequest("PUT", endpoint)
        connection.putheader("Content-Type", "application/gzip")
        connection.putheader("Content-Length", str(size))
        connection.putheader("X-API-Key", _api_key())
        connection.putheader("User-Agent", "lexico-system-monitor/1.0")
        connection.endheaders()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        response = connection.getresponse()
        response.read()
        if response.status >= 300:
            raise RuntimeError(f"bundle API returned HTTP {response.status}")
    finally:
        connection.close()


def _diagnostic_command(command, timeout=10):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return output.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"command failed: {exc}"


def _diagnostic_snapshot(label):
    commands = [
        ("uptime", ["uptime"]),
        ("memory", ["free", "-m"]),
        ("vmstat", ["vmstat", "1", "3"]),
        ("network links", ["ip", "-s", "link"]),
        ("socket summary", ["ss", "-s"]),
        ("udp counters", ["netstat", "-su"]),
        ("FreeSWITCH status", ["fs_cli", "-x", "status"]),
        ("FreeSWITCH channels", ["fs_cli", "-x", "show channels count"]),
    ]
    sections = [f"# {label} - {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"]
    for title, command in commands:
        sections.append(f"\n## {title}\n{_diagnostic_command(command)}")
    for path in (Path("/proc/net/softnet_stat"), Path("/proc/net/snmp"), Path("/proc/net/netstat")):
        try:
            sections.append(f"\n## {path}\n{path.read_text(encoding='ascii', errors='replace')}")
        except OSError as exc:
            sections.append(f"\n## {path}\nread failed: {exc}")
    return "\n".join(sections)


class IncidentCaptureManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.last_trigger = 0.0

    def maybe_trigger(self, payload):
        if not AUTOMATIC_INCIDENTS_ENABLED:
            return False
        reasons = incident_reasons(payload)
        if not reasons:
            return False
        now = time.monotonic()
        with self.lock:
            if self.running or now - self.last_trigger < INCIDENT_COOLDOWN_SECONDS:
                return False
            self.running = True
            self.last_trigger = now
        threading.Thread(
            target=self._capture,
            args=(dict(payload), reasons),
            daemon=True,
        ).start()
        return True

    def _capture(self, payload, reasons):
        incident_id = None
        work_dir = None
        try:
            incident = {
                "triggered_at": payload.get("sampled_at") or "",
                "source": payload.get("source") or SOURCE,
                "voice_node": payload.get("voice_node") or configured_voice_node(),
                "trigger_reason": "; ".join(reasons),
                "suspected_cause": suspected_cause(payload, reasons),
                "cpu_percent": payload.get("cpu_percent") or 0,
                "cpu_steal_percent": payload.get("cpu_steal_percent") or 0,
                "active_calls": payload.get("active_calls") or 0,
                "packet_loss_percent": payload.get("packet_loss_percent"),
                "jitter_ms": payload.get("jitter_ms"),
                "rtp_packets": payload.get("rtp_packets") or 0,
                "rtp_legs": payload.get("rtp_legs") or 0,
            }
            created = _request_json("/api/system-monitoring/incidents", incident)
            incident_id = int(created["incident_id"])

            INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix=f"incident-{incident_id}-", dir=INCIDENT_DIR))
            pcap_path = work_dir / "voice-traffic.pcap"
            metadata_path = work_dir / "metadata.json"
            diagnostics_path = work_dir / "diagnostics.txt"
            tcpdump_log = work_dir / "tcpdump.log"

            metadata = {
                **incident,
                "incident_id": incident_id,
                "capture_duration_limit_seconds": INCIDENT_DURATION_SECONDS,
                "capture_size_limit_bytes": INCIDENT_MAX_BYTES,
                "snaplen": 128,
                "bpf_filter": INCIDENT_BPF_FILTER,
            }
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            diagnostics_path.write_text(_diagnostic_snapshot("before capture"), encoding="utf-8")

            command = [
                "tcpdump", "-i", "any", "-nn", "-U", "-s", "128", "-B", "8192",
                "-w", str(pcap_path), *shlex.split(INCIDENT_BPF_FILTER),
            ]
            with tcpdump_log.open("wb") as log_handle:
                process = subprocess.Popen(command, stdout=log_handle, stderr=log_handle)
                capture_started = time.monotonic()
                while process.poll() is None:
                    elapsed = time.monotonic() - capture_started
                    size = pcap_path.stat().st_size if pcap_path.exists() else 0
                    if elapsed >= INCIDENT_DURATION_SECONDS or size >= INCIDENT_MAX_BYTES:
                        process.terminate()
                        break
                    time.sleep(1)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

            diagnostics_path.write_text(
                diagnostics_path.read_text(encoding="utf-8")
                + "\n\n"
                + _diagnostic_snapshot("after capture"),
                encoding="utf-8",
            )
            if not pcap_path.exists() or pcap_path.stat().st_size < 24:
                raise RuntimeError("tcpdump did not create a usable PCAP")

            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            archive = INCIDENT_DIR / f"incident-{incident_id}-{incident['voice_node']}-{stamp}.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for path in sorted(work_dir.iterdir()):
                    bundle.add(path, arcname=path.name)
            _upload_bundle(incident_id, archive.read_bytes())
            self._prune_local_archives()
            print(f"incident {incident_id} captured: {archive}", flush=True)
        except Exception as exc:
            print(f"incident capture failed: {exc}", flush=True)
            if incident_id is not None:
                try:
                    _request_json(
                        f"/api/system-monitoring/incidents/{incident_id}/failed",
                        {"error": str(exc)},
                    )
                except Exception as report_exc:
                    print(f"incident failure report failed: {report_exc}", flush=True)
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
            with self.lock:
                self.running = False

    def _prune_local_archives(self):
        archives = sorted(
            INCIDENT_DIR.glob("*.tar.gz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in archives[INCIDENT_LOCAL_FILES:]:
            try:
                path.unlink()
            except OSError:
                pass


def _fs_cli(command, timeout=8):
    return _diagnostic_command(["fs_cli", "-x", command], timeout=timeout)


def _active_channel_rows():
    try:
        result = subprocess.run(
            ["fs_cli", "-x", "show channels as json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return json.loads(result.stdout or "{}").get("rows") or []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def _uuid_dump(uuid):
    if not re.fullmatch(r"[0-9A-Fa-f-]{32,40}", str(uuid or "")):
        return {}
    output = _fs_cli(f"uuid_dump {uuid}")
    values = {}
    for line in output.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        else:
            continue
        values[key.strip()] = unquote(value.strip())
    return values


def _safe_fragment(value, fallback="call"):
    clean = re.sub(r"[^0-9A-Za-z_-]+", "-", str(value or "")).strip("-")
    return (clean or fallback)[:48]


def _channel_matches_vendor(values, vendor_ips, gateway_names):
    direction = str(
        values.get("Call-Direction")
        or values.get("variable_direction")
        or values.get("Channel-Call-Direction")
        or ""
    ).lower()
    if direction and direction != "outbound":
        return False
    ip_values = "\n".join(
        str(values.get(key) or "")
        for key in (
            "variable_sip_network_ip",
            "variable_sip_from_host",
            "variable_sip_req_host",
            "variable_remote_media_ip",
            "Channel-Name",
        )
    ).casefold()
    if any(ip.casefold() in ip_values for ip in vendor_ips):
        return True
    gateway_values = "\n".join(
        str(values.get(key) or "")
        for key in (
            "variable_sip_gateway_name",
            "variable_gateway_name",
            "variable_sofia_gateway_name",
            "Channel-Name",
            "variable_channel_name",
        )
    ).casefold()
    return any(name.casefold() in gateway_values for name in gateway_names)


def _vendor_addresses(values, vendor_ips):
    addresses = list(vendor_ips)
    for key in (
        "variable_remote_media_ip",
        "variable_sip_network_ip",
        "variable_sip_from_host",
        "variable_sip_req_host",
    ):
        value = str(values.get(key) or "").strip()
        try:
            socket.inet_pton(socket.AF_INET, value)
        except OSError:
            continue
        if value not in addresses:
            addresses.append(value)
    return addresses


def _start_vendor_tcpdump(path, addresses, log_path):
    if not addresses:
        return None
    expression = "udp and (" + " or ".join(f"host {address}" for address in addresses) + ")"
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [
                "tcpdump", "-i", "any", "-nn", "-U", "-s", "192", "-B", "8192",
                "-w", str(path), *shlex.split(expression),
            ],
            stdout=log_handle,
            stderr=log_handle,
        )
    except Exception:
        log_handle.close()
        raise
    process._lexico_log_handle = log_handle
    return process


def _stop_capture_process(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    log_handle = getattr(process, "_lexico_log_handle", None)
    if log_handle is not None:
        log_handle.close()


class ManualCaptureManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False

    def poll(self, payload):
        with self.lock:
            if self.running:
                return False
        result = _get_json(
            "/api/system-monitoring/capture-commands/next",
            {"voice_node": payload["voice_node"]},
        )
        command = result.get("command")
        if not command:
            return False
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(
            target=self._run,
            args=(command, dict(payload)),
            daemon=True,
        ).start()
        return True

    def _run(self, command, payload):
        request_id = int(command["id"])
        incident_id = None
        work_dir = None
        processes = []
        recordings = {}
        try:
            duration = max(60, min(600, int(command.get("duration_seconds") or 600)))
            max_recordings = max(1, min(2, int(command.get("max_recordings") or 2)))
            vendor_name = str(command.get("vendor_name") or "Vendor")
            vendor_ips = [str(value) for value in command.get("vendor_ips") or []]
            gateway_names = [str(value) for value in command.get("gateway_names") or []]
            incident = {
                "triggered_at": payload.get("sampled_at") or "",
                "source": payload.get("source") or SOURCE,
                "voice_node": payload.get("voice_node") or configured_voice_node(),
                "trigger_reason": f"Manual 10-minute vendor capture: {vendor_name}",
                "suspected_cause": "Ручной сбор: PCAP только по ноге вендора и до двух WAV-записей.",
                "cpu_percent": payload.get("cpu_percent") or 0,
                "cpu_steal_percent": payload.get("cpu_steal_percent") or 0,
                "active_calls": payload.get("active_calls") or 0,
                "packet_loss_percent": payload.get("packet_loss_percent"),
                "jitter_ms": payload.get("jitter_ms"),
                "rtp_packets": payload.get("rtp_packets") or 0,
                "rtp_legs": payload.get("rtp_legs") or 0,
            }
            created = _request_json("/api/system-monitoring/incidents", incident)
            incident_id = int(created["incident_id"])

            INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix=f"manual-{request_id}-", dir=INCIDENT_DIR))
            work_dir.chmod(0o777)
            tcpdump_log = work_dir / "tcpdump.log"
            diagnostics_path = work_dir / "diagnostics.txt"
            diagnostics_path.write_text(_diagnostic_snapshot("manual capture start"), encoding="utf-8")
            metadata = {
                "request_id": request_id,
                "incident_id": incident_id,
                "voice_node": incident["voice_node"],
                "vendor_name": vendor_name,
                "vendor_ips": vendor_ips,
                "gateway_names": gateway_names,
                "duration_seconds": duration,
                "max_recordings": max_recordings,
                "client_leg_excluded": True,
            }
            (work_dir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            vendor_pcap = work_dir / f"vendor-{_safe_fragment(vendor_name)}.pcap"
            vendor_process = _start_vendor_tcpdump(vendor_pcap, vendor_ips, tcpdump_log)
            if vendor_process is not None:
                processes.append(vendor_process)

            deadline = time.monotonic() + duration
            examined = {}
            while time.monotonic() < deadline:
                rows = _active_channel_rows()
                active_uuids = {
                    str(row.get("uuid") or "")
                    for row in rows
                    if str(row.get("callstate") or "").upper() == "ACTIVE"
                }
                active_uuids.discard("")

                for uuid, recording in list(recordings.items()):
                    if uuid not in active_uuids or time.monotonic() >= recording["record_until"]:
                        _stop_capture_process(recording.get("process"))
                        recording["process"] = None
                        recording["finished"] = True

                recording_in_progress = any(
                    not recording.get("finished") for recording in recordings.values()
                )
                if len(recordings) < max_recordings and not recording_in_progress:
                    for row in rows:
                        if str(row.get("callstate") or "").upper() != "ACTIVE":
                            continue
                        uuid = str(row.get("uuid") or "")
                        if not uuid or uuid in recordings:
                            continue
                        if time.monotonic() - examined.get(uuid, 0) < 5:
                            continue
                        examined[uuid] = time.monotonic()
                        values = _uuid_dump(uuid)
                        if not _channel_matches_vendor(values, vendor_ips, gateway_names):
                            continue
                        number = (
                            values.get("variable_destination_number")
                            or values.get("Caller-Destination-Number")
                            or row.get("dest")
                            or uuid[:8]
                        )
                        index = len(recordings) + 1
                        base_name = f"call-{index}-{_safe_fragment(number)}-{uuid[:8]}"
                        wav_path = work_dir / f"{base_name}.wav"
                        remaining = max(1, int(deadline - time.monotonic()))
                        record_limit = min(300, remaining)
                        response = _fs_cli(f"uuid_record {uuid} start {wav_path} {record_limit}")
                        if "ERR" in response.upper():
                            print(f"manual recording failed uuid={uuid}: {response}", flush=True)
                            continue
                        addresses = _vendor_addresses(values, vendor_ips)
                        call_pcap = work_dir / f"{base_name}-vendor.pcap"
                        process = _start_vendor_tcpdump(call_pcap, addresses, tcpdump_log)
                        if process is not None:
                            processes.append(process)
                        recordings[uuid] = {
                            "wav": wav_path,
                            "pcap": call_pcap,
                            "process": process,
                            "addresses": addresses,
                            "record_until": time.monotonic() + record_limit,
                            "finished": False,
                        }
                        if len(recordings) >= max_recordings:
                            break

                for process in processes:
                    if process.poll() is None:
                        path_index = process.args.index("-w") + 1
                        path = Path(process.args[path_index])
                        if path.exists() and path.stat().st_size >= MANUAL_CAPTURE_FILE_BYTES:
                            _stop_capture_process(process)
                time.sleep(2)

            for uuid, recording in recordings.items():
                if not recording.get("finished"):
                    _fs_cli(f"uuid_record {uuid} stop {recording['wav']}")
            for process in processes:
                _stop_capture_process(process)

            diagnostics_path.write_text(
                diagnostics_path.read_text(encoding="utf-8")
                + "\n\n"
                + _diagnostic_snapshot("manual capture end"),
                encoding="utf-8",
            )
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            archive = INCIDENT_DIR / f"manual-{request_id}-{incident['voice_node']}-{stamp}.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for path in sorted(work_dir.iterdir()):
                    if path.is_file() and path.stat().st_size > 0:
                        bundle.add(path, arcname=path.name)
            _upload_bundle_file(incident_id, archive)
            _request_json(
                f"/api/system-monitoring/capture-commands/{request_id}/complete",
                {"incident_id": incident_id},
            )
            IncidentCaptureManager()._prune_local_archives()
            print(
                f"manual capture request={request_id} incident={incident_id} "
                f"recordings={len(recordings)} complete",
                flush=True,
            )
        except Exception as exc:
            print(f"manual capture failed request={request_id}: {exc}", flush=True)
            for process in processes:
                _stop_capture_process(process)
            if incident_id is not None:
                try:
                    _request_json(
                        f"/api/system-monitoring/incidents/{incident_id}/failed",
                        {"error": str(exc)},
                    )
                except Exception:
                    pass
            try:
                _request_json(
                    f"/api/system-monitoring/capture-commands/{request_id}/failed",
                    {"incident_id": incident_id, "error": str(exc)},
                )
            except Exception as report_exc:
                print(f"manual capture failure report failed: {report_exc}", flush=True)
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
            with self.lock:
                self.running = False


def main():
    accumulator = QualityAccumulator()
    incident_manager = IncidentCaptureManager()
    manual_capture_manager = ManualCaptureManager()
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
            "voice_node": configured_voice_node(),
            "cpu_percent": usage,
            "cpu_steal_percent": steal,
            "active_calls": active_calls(),
            **quality,
        }
        sent = False
        try:
            post_sample(payload)
            sent = True
            print(
                f"sent node={payload['voice_node']} cpu={usage:.1f}% calls={payload['active_calls']} "
                f"rtp_legs={quality['rtp_legs']}",
                flush=True,
            )
        except Exception as exc:
            accumulator.restore(quality)
            print(f"monitor API error: {exc}", flush=True)
        if sent:
            try:
                incident_manager.maybe_trigger(payload)
                manual_capture_manager.poll(payload)
            except Exception as exc:
                print(f"capture command API error: {exc}", flush=True)
        time.sleep(max(1, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
