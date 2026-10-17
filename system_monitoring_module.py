"""System and RTP quality monitoring supplied by the voice server agent."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import voice_nodes


HISTORY_HOURS = 6
QUALITY_WINDOW_MINUTES = 5
STALE_AFTER_SECONDS = 60


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
    finally:
        conn.close()

    return {
        "online": any(node["online"] for node in nodes),
        "all_online": all(node["online"] for node in nodes),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
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

    @app.get("/api/system-monitoring", dependencies=main.ADMIN_AUTH)
    def system_monitoring_data():
        return system_snapshot(db)
