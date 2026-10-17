#!/usr/bin/env python3
"""Collect low-overhead CPU, active-call and post-call RTP quality metrics."""

import json
import math
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from urllib.parse import unquote
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
            INCIDENT_DIR.glob("incident-*.tar.gz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in archives[INCIDENT_LOCAL_FILES:]:
            try:
                path.unlink()
            except OSError:
                pass


def main():
    accumulator = QualityAccumulator()
    incident_manager = IncidentCaptureManager()
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
        try:
            post_sample(payload)
            incident_manager.maybe_trigger(payload)
            print(
                f"sent node={payload['voice_node']} cpu={usage:.1f}% calls={payload['active_calls']} "
                f"rtp_legs={quality['rtp_legs']}",
                flush=True,
            )
        except Exception as exc:
            accumulator.restore(quality)
            print(f"monitor API error: {exc}", flush=True)
        time.sleep(max(1, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
