"""System and RTP quality monitoring supplied by the voice server agent."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import voice_nodes


HISTORY_HOURS = 6
QUALITY_WINDOW_MINUTES = 5
STALE_AFTER_SECONDS = 60
MAX_INCIDENT_BUNDLE_BYTES = 12 * 1024 * 1024
INCIDENT_FILES_TO_KEEP = 20


class SystemMetricIn(BaseModel):
    sampled_at: str = ""
    source: str = "voip"
    voice_node: str = voice_nodes.PRIMARY_NODE
    cpu_percent: float = Field(ge=0, le=100)
    cpu_steal_percent: float = Field(default=0, ge=0, le=100)
    active_calls: int = Field(default=0, ge=0)
    packet_loss_percent: Optional[float] = Field(default=None, ge=0, le=100)
    jitter_ms: Optional[float] = Field(default=None, ge=0)
    rtp_packets: int = Field(default=0, ge=0)
    rtp_legs: int = Field(default=0, ge=0)


class SystemIncidentIn(BaseModel):
    triggered_at: str = ""
    source: str = "voip"
    voice_node: str = voice_nodes.PRIMARY_NODE
    trigger_reason: str = ""
    suspected_cause: str = ""
    cpu_percent: float = Field(default=0, ge=0, le=100)
    cpu_steal_percent: float = Field(default=0, ge=0, le=100)
    active_calls: int = Field(default=0, ge=0)
    packet_loss_percent: Optional[float] = Field(default=None, ge=0, le=100)
    jitter_ms: Optional[float] = Field(default=None, ge=0)
    rtp_packets: int = Field(default=0, ge=0)
    rtp_legs: int = Field(default=0, ge=0)


class IncidentFailureIn(BaseModel):
    error: str = "capture failed"


class IncidentNotificationIn(BaseModel):
    kind: str


def init_schema(db):
    conn = db.get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS system_metric_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sampled_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'voip',
                voice_node TEXT NOT NULL DEFAULT 'primary',
                cpu_percent REAL NOT NULL,
                cpu_steal_percent REAL NOT NULL DEFAULT 0,
                active_calls INTEGER NOT NULL DEFAULT 0,
                packet_loss_percent REAL,
                jitter_ms REAL,
                rtp_packets INTEGER NOT NULL DEFAULT 0,
                rtp_legs INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS system_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                triggered_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'voip',
                voice_node TEXT NOT NULL DEFAULT 'primary',
                trigger_reason TEXT NOT NULL DEFAULT '',
                suspected_cause TEXT NOT NULL DEFAULT '',
                cpu_percent REAL NOT NULL DEFAULT 0,
                cpu_steal_percent REAL NOT NULL DEFAULT 0,
                active_calls INTEGER NOT NULL DEFAULT 0,
                packet_loss_percent REAL,
                jitter_ms REAL,
                rtp_packets INTEGER NOT NULL DEFAULT 0,
                rtp_legs INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'capturing',
                error TEXT NOT NULL DEFAULT '',
                bundle_name TEXT NOT NULL DEFAULT '',
                bundle_path TEXT NOT NULL DEFAULT '',
                bundle_size INTEGER NOT NULL DEFAULT 0,
                alert_sent_at TEXT,
                bundle_sent_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_system_incidents_created
                ON system_incidents(created_at DESC);
            """
        )
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(system_metric_samples)")
        }
        if "voice_node" not in columns:
            conn.execute(
                "ALTER TABLE system_metric_samples "
                "ADD COLUMN voice_node TEXT NOT NULL DEFAULT 'primary'"
            )
        conn.execute(
            "UPDATE system_metric_samples SET voice_node = ? "
            "WHERE voice_node IS NULL OR trim(voice_node) = ''",
            (voice_nodes.PRIMARY_NODE,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_system_metric_sampled "
            "ON system_metric_samples(sampled_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_system_metric_node_sampled "
            "ON system_metric_samples(voice_node, sampled_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _normalized_timestamp(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def record_sample(db, sample: SystemMetricIn):
    conn = db.get_conn()
    try:
        conn.execute(
            """
            INSERT INTO system_metric_samples
                (sampled_at, source, voice_node, cpu_percent, cpu_steal_percent, active_calls,
                 packet_loss_percent, jitter_ms, rtp_packets, rtp_legs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _normalized_timestamp(sample.sampled_at),
                (sample.source or "voip")[:64],
                voice_nodes.normalize(sample.voice_node),
                round(float(sample.cpu_percent), 3),
                round(float(sample.cpu_steal_percent), 3),
                int(sample.active_calls),
                None if sample.packet_loss_percent is None else round(float(sample.packet_loss_percent), 4),
                None if sample.jitter_ms is None else round(float(sample.jitter_ms), 3),
                int(sample.rtp_packets),
                int(sample.rtp_legs),
            ),
        )
        conn.execute(
            "DELETE FROM system_metric_samples WHERE sampled_at < datetime('now', '-48 hours')"
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _incident_directory(db):
    directory = db.DB_PATH.parent / "system-incidents"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def create_incident(db, incident: SystemIncidentIn):
    conn = db.get_conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO system_incidents
                (triggered_at, source, voice_node, trigger_reason, suspected_cause,
                 cpu_percent, cpu_steal_percent, active_calls, packet_loss_percent,
                 jitter_ms, rtp_packets, rtp_legs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _normalized_timestamp(incident.triggered_at),
                (incident.source or "voip")[:64],
                voice_nodes.normalize(incident.voice_node),
                (incident.trigger_reason or "")[:500],
                (incident.suspected_cause or "")[:1000],
                round(float(incident.cpu_percent), 3),
                round(float(incident.cpu_steal_percent), 3),
                int(incident.active_calls),
                None if incident.packet_loss_percent is None else round(float(incident.packet_loss_percent), 4),
                None if incident.jitter_ms is None else round(float(incident.jitter_ms), 3),
                int(incident.rtp_packets),
                int(incident.rtp_legs),
            ),
        )
        conn.commit()
        return {"ok": True, "incident_id": cursor.lastrowid}
    finally:
        conn.close()


def _incident_row(db, incident_id):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM system_incidents WHERE id = ?", (int(incident_id),)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _prune_incident_files(db):
    conn = db.get_conn()
    try:
        expired = conn.execute(
            "SELECT id, bundle_path FROM system_incidents "
            "WHERE bundle_path != '' ORDER BY id DESC LIMIT -1 OFFSET ?",
            (INCIDENT_FILES_TO_KEEP,),
        ).fetchall()
        for row in expired:
            try:
                Path(row["bundle_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute(
                "UPDATE system_incidents SET status = 'expired', bundle_path = '', "
                "bundle_size = 0, updated_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
        conn.execute(
            "DELETE FROM system_incidents WHERE created_at < datetime('now', '-30 days')"
        )
        conn.commit()
    finally:
        conn.close()


def store_incident_bundle(db, incident_id, content):
    incident = _incident_row(db, incident_id)
    if incident is None:
        raise HTTPException(404, "Incident not found")
    if not content:
        raise HTTPException(400, "Empty incident bundle")
    if len(content) > MAX_INCIDENT_BUNDLE_BYTES:
        raise HTTPException(413, "Incident bundle is too large")

    node = voice_nodes.normalize(incident["voice_node"])
    file_name = f"incident-{int(incident_id)}-{node}.tar.gz"
    path = _incident_directory(db) / file_name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)

    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE system_incidents SET status = 'ready', error = '', bundle_name = ?, "
            "bundle_path = ?, bundle_size = ?, updated_at = datetime('now') WHERE id = ?",
            (file_name, str(path), len(content), int(incident_id)),
        )
        conn.commit()
    finally:
        conn.close()
    _prune_incident_files(db)
    return {"ok": True, "incident_id": int(incident_id), "bundle_size": len(content)}


def fail_incident(db, incident_id, error):
    conn = db.get_conn()
    try:
        cursor = conn.execute(
            "UPDATE system_incidents SET status = 'failed', error = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            ((error or "capture failed")[:1000], int(incident_id)),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Incident not found")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def pending_incidents(db):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM system_incidents
               WHERE alert_sent_at IS NULL
                  OR (status IN ('ready', 'failed') AND bundle_sent_at IS NULL)
               ORDER BY id ASC LIMIT 20"""
        ).fetchall()
        incidents = []
        for row in rows:
            incident = dict(row)
            incident.pop("bundle_path", None)
            incidents.append(incident)
        return incidents
    finally:
        conn.close()


def mark_incident_notified(db, incident_id, kind):
    column = {"alert": "alert_sent_at", "bundle": "bundle_sent_at"}.get(kind)
    if column is None:
        raise HTTPException(422, "Unknown notification kind")
    conn = db.get_conn()
    try:
        cursor = conn.execute(
            f"UPDATE system_incidents SET {column} = datetime('now'), "
            "updated_at = datetime('now') WHERE id = ?",
            (int(incident_id),),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Incident not found")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _weighted_quality(rows, field):
    available = [row for row in rows if row[field] is not None and int(row["rtp_packets"] or 0) > 0]
    packets = sum(int(row["rtp_packets"] or 0) for row in available)
    if not available or packets <= 0:
        return None
    return round(
        sum(float(row[field]) * int(row["rtp_packets"]) for row in available) / packets,
        3,
    )


def _node_snapshot(conn, node):
    node_id = node["id"]
    latest_row = conn.execute(
        "SELECT * FROM system_metric_samples WHERE voice_node = ? "
        "ORDER BY sampled_at DESC, id DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    history_rows = conn.execute(
            """
            SELECT sampled_at, cpu_percent, cpu_steal_percent, active_calls,
                   packet_loss_percent, jitter_ms, rtp_packets, rtp_legs
            FROM system_metric_samples
            WHERE voice_node = ? AND sampled_at >= datetime('now', ?)
            ORDER BY sampled_at ASC, id ASC
            LIMIT 1440
            """,
            (node_id, f"-{HISTORY_HOURS} hours"),
    ).fetchall()
    quality_rows = conn.execute(
            """
            SELECT packet_loss_percent, jitter_ms, rtp_packets, rtp_legs
            FROM system_metric_samples
            WHERE voice_node = ? AND sampled_at >= datetime('now', ?)
            """,
            (node_id, f"-{QUALITY_WINDOW_MINUTES} minutes"),
    ).fetchall()

    if latest_row is None:
        return {
            **node,
            "online": False,
            "stale": True,
            "latest": None,
            "quality": {"packet_loss_percent": None, "jitter_ms": None, "rtp_packets": 0, "rtp_legs": 0},
            "history": [],
        }

    latest = dict(latest_row)
    sampled = datetime.strptime(latest["sampled_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((datetime.now(timezone.utc) - sampled).total_seconds()))
    quality = {
        "packet_loss_percent": _weighted_quality(quality_rows, "packet_loss_percent"),
        "jitter_ms": _weighted_quality(quality_rows, "jitter_ms"),
        "rtp_packets": sum(int(row["rtp_packets"] or 0) for row in quality_rows),
        "rtp_legs": sum(int(row["rtp_legs"] or 0) for row in quality_rows),
    }
    return {
        **node,
        "online": age_seconds <= STALE_AFTER_SECONDS,
        "stale": age_seconds > STALE_AFTER_SECONDS,
        "age_seconds": age_seconds,
        "latest": latest,
        "quality": quality,
        "history": [dict(row) for row in history_rows],
    }


def system_snapshot(db):
    conn = db.get_conn()
    try:
        nodes = [_node_snapshot(conn, node) for node in voice_nodes.list_nodes()]
        incident_rows = conn.execute(
            """SELECT id, triggered_at, voice_node, trigger_reason, suspected_cause,
                      cpu_percent, cpu_steal_percent, active_calls,
                      packet_loss_percent, jitter_ms, status, error, bundle_name,
                      bundle_size
               FROM system_incidents ORDER BY id DESC LIMIT 10"""
        ).fetchall()
    finally:
        conn.close()

    return {
        "online": any(node["online"] for node in nodes),
        "all_online": all(node["online"] for node in nodes),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "incidents": [dict(row) for row in incident_rows],
        "history_hours": HISTORY_HOURS,
        "quality_window_minutes": QUALITY_WINDOW_MINUTES,
    }


def install(app, main, db, base_path: Path):
    init_schema(db)

    @app.on_event("startup")
    def _system_monitoring_startup():
        init_schema(db)

    @app.get("/system", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def system_monitoring_page(request: Request):
        return HTMLResponse(
            (base_path / "system_monitoring.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.post("/api/system-monitoring/ingest", dependencies=main.API_AUTH)
    def system_monitoring_ingest(sample: SystemMetricIn):
        return record_sample(db, sample)

    @app.post("/api/system-monitoring/incidents", dependencies=main.API_AUTH)
    def system_monitoring_create_incident(incident: SystemIncidentIn):
        return create_incident(db, incident)

    @app.put("/api/system-monitoring/incidents/{incident_id}/bundle", dependencies=main.API_AUTH)
    async def system_monitoring_store_bundle(incident_id: int, request: Request):
        return store_incident_bundle(db, incident_id, await request.body())

    @app.post("/api/system-monitoring/incidents/{incident_id}/failed", dependencies=main.API_AUTH)
    def system_monitoring_fail_incident(incident_id: int, failure: IncidentFailureIn):
        return fail_incident(db, incident_id, failure.error)

    @app.get("/api/ops/system-incidents/pending", dependencies=main.API_AUTH)
    def system_monitoring_pending_incidents():
        return {"incidents": pending_incidents(db)}

    @app.post("/api/ops/system-incidents/{incident_id}/notified", dependencies=main.API_AUTH)
    def system_monitoring_mark_notified(incident_id: int, notification: IncidentNotificationIn):
        return mark_incident_notified(db, incident_id, notification.kind)

    @app.get("/api/ops/system-incidents/{incident_id}/bundle", dependencies=main.API_AUTH)
    def system_monitoring_download_bundle(incident_id: int):
        incident = _incident_row(db, incident_id)
        if incident is None:
            raise HTTPException(404, "Incident not found")
        if incident["status"] != "ready" or not incident["bundle_path"]:
            raise HTTPException(409, "Incident bundle is not ready")
        path = Path(incident["bundle_path"])
        if not path.is_file() or path.parent.resolve() != _incident_directory(db).resolve():
            raise HTTPException(404, "Incident bundle not found")
        return FileResponse(
            path,
            filename=incident["bundle_name"] or path.name,
            media_type="application/gzip",
        )

    @app.get("/api/system-monitoring", dependencies=main.ADMIN_AUTH)
    def system_monitoring_data():
        return system_snapshot(db)
