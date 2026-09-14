"""Independent, bounded SIP archive. No billing database or call-control imports."""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from email.parser import Parser
from contextlib import contextmanager


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sip_user(value):
    match = re.search(r"(?:sips?|tel):([^@;>\s]+)", value or "", re.I)
    return match.group(1).lstrip("+") if match else ""


def parse_sip(payload, src, dst, local_ips, observed_at):
    text = payload.decode("utf-8", "replace")
    first, _, rest = text.partition("\r\n")
    if not rest:
        first, _, rest = text.partition("\n")
    headers = Parser().parsestr(rest, headersonly=True)
    status = first.split(" ", 2) if first.startswith("SIP/2.0 ") else []
    request = re.fullmatch(r"(INVITE|CANCEL|BYE) (\S+) SIP/2.0", first)
    cseq = headers.get("CSeq", "").strip()
    # Do not archive registrations, authentication headers, SDP bodies or media.
    if not request and not (status and cseq.endswith((" INVITE", " BYE", " CANCEL"))):
        return None
    call_id = headers.get("Call-ID") or headers.get("i")
    if not call_id or len(call_id) > 512:
        return None
    return {
        "at": observed_at, "direction": "out" if src[0] in local_ips else "in" if dst[0] in local_ips else "unknown",
        "src_ip": src[0], "src_port": src[1], "dst_ip": dst[0], "dst_port": dst[1],
        "call_id": call_id, "cseq": cseq[:100], "method": request[1] if request else "",
        "b": sip_user(request[2]) if request else "",
        "a": sip_user(headers.get("From") or headers.get("f") or ""),
        "pai": sip_user(headers.get("P-Asserted-Identity", "")),
        "rpid": sip_user(headers.get("Remote-Party-ID", "")),
        "status": " ".join(status[1:])[:200], "reason": headers.get("Reason", "")[:300],
    }


def channel_event(event):
    uid = event.get("Unique-ID")
    if not uid:
        return None
    keys = {
        "uuid": "Unique-ID", "call_uuid": "Channel-Call-UUID", "sip_id": "variable_sip_call_id",
        "origin_uuid": "variable_originating_leg_uuid", "peer_uuid": "Other-Leg-Unique-ID",
        "direction": "Call-Direction", "ip": "variable_sip_network_ip",
        "b": "Caller-Destination-Number", "cause": "Hangup-Cause", "seconds": "variable_billsec",
    }
    result = {key: str(event.get(source) or "")[:512] for key, source in keys.items()}
    result["peer_uuid"] = result["peer_uuid"] or event.get("variable_bridge_uuid") or event.get("variable_signal_bond") or ""
    result["ip"] = result["ip"] or event.get("Caller-Network-Addr") or ""
    result["at"] = utcnow()
    created = event.get("Caller-Channel-Created-Time") or event.get("Channel-Created-Time")
    if str(created or "").isdigit() and int(created) > 0:
        result["started_at"] = datetime.fromtimestamp(int(created) / 1000000, timezone.utc).isoformat(timespec="seconds")
    return result


class Archive:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                PRAGMA auto_vacuum=INCREMENTAL;
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    fingerprint TEXT PRIMARY KEY, at TEXT NOT NULL, call_id TEXT NOT NULL,
                    method TEXT NOT NULL, b TEXT NOT NULL, a TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_day ON events(at);
                CREATE INDEX IF NOT EXISTS events_call ON events(call_id);
                CREATE TABLE IF NOT EXISTS channels (
                    uuid TEXT PRIMARY KEY, sip_id TEXT, call_uuid TEXT, origin_uuid TEXT,
                    peer_uuid TEXT, at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS channels_sip ON channels(sip_id);
                CREATE INDEX IF NOT EXISTS channels_call ON channels(call_uuid);
                CREATE INDEX IF NOT EXISTS channels_origin ON channels(origin_uuid);
                CREATE INDEX IF NOT EXISTS channels_peer ON channels(peer_uuid);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('started_at', ?)", (utcnow(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def add(self, events=(), channels=()):
        with self.connect() as db:
            for event in events:
                semantic = {key: value for key, value in event.items() if key != "at"}
                digest = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)",
                           (digest, event["at"], event["call_id"], event["method"], event["b"], event["a"], json.dumps(event)))
            for item in channels:
                old = db.execute("SELECT data FROM channels WHERE uuid=?", (item["uuid"],)).fetchone()
                merged = json.loads(old[0]) if old else {}
                merged.update({key: value for key, value in item.items() if value != ""})
                db.execute("INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?,?)",
                           (*[merged.get(k, "") for k in ("uuid", "sip_id", "call_uuid", "origin_uuid", "peer_uuid", "at")], json.dumps(merged)))

    def prune(self, days=7):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE at<?", (cutoff,))
            db.execute("DELETE FROM channels WHERE at<?", (cutoff,))
            db.commit()
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            db.execute("PRAGMA incremental_vacuum(4096)")

    def query(self, number, day):
        if not re.fullmatch(r"[0-9]{7,24}", number):
            raise ValueError("Invalid number")
        date = datetime.strptime(day, "%Y-%m-%d").date()
        if day != date.isoformat():
            raise ValueError("Invalid date")
        start, end = day + "T00:00:00", (date + timedelta(days=1)).isoformat() + "T00:00:00"
        with self.connect() as db:
            # Suffix matching preserves technical prefixes in the displayed wire B.
            seeds = db.execute("SELECT data FROM events WHERE at>=? AND at<? AND method='INVITE' AND (b LIKE ? OR a=?) ORDER BY at",
                               (start, end, "%" + number, number)).fetchall()
            events = [json.loads(row[0]) for row in seeds]
            channels = {}
            ids = {event["call_id"] for event in events}
            for call_id in ids:
                for row in db.execute("SELECT data FROM channels WHERE sip_id=? OR uuid=?", (call_id, call_id)):
                    item = json.loads(row[0])
                    channels[item["uuid"]] = item
            # Only explicit FreeSWITCH identifiers link legs. Never infer by time or A/B alone.
            for _ in range(3):
                found = list(channels.values())
                for item in found:
                    links = {item.get(k) for k in ("uuid", "call_uuid", "origin_uuid", "peer_uuid")} - {"", None}
                    for link in links:
                        for row in db.execute("SELECT data FROM channels WHERE uuid=? OR call_uuid=? OR origin_uuid=? OR peer_uuid=?", (link,) * 4):
                            record = json.loads(row[0])
                            channels[record["uuid"]] = record
            ids.update(item["sip_id"] for item in channels.values() if item.get("sip_id"))
            ids.update(channels)
            events = []
            # Explicitly flag the exceptional size cap, never silently truncate a daily report.
            limited = False
            for call_id in sorted(ids):
                events.extend(json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE call_id=? ORDER BY at", (call_id,)))
                if len(events) > 20000:
                    limited = True
                    break
            return {"number": number, "day": day, "events": events, "channels": list(channels.values()),
                    "meta": dict(db.execute("SELECT key,value FROM meta")), "limited": limited}
