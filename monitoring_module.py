"""Profitability history and low-overhead route traffic-drop monitoring."""

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import voice_nodes


_CACHE_TTL_SECONDS = 60
_MAX_PERIOD_DAYS = 366
_cache_lock = threading.Lock()
_cache = {}


class TrafficDropNotificationIn(BaseModel):
    kind: str = "alert"


def _positive_int_env(name, default, minimum=1):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def traffic_drop_settings():
    return {
        "threshold_calls": _positive_int_env("TRAFFIC_DROP_PEAK_CALLS", 10),
        "silence_seconds": _positive_int_env("TRAFFIC_DROP_SILENCE_SECONDS", 300, 60),
    }


def init_schema(db):
    conn = db.get_conn()
    try:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_cdr_started ON cdr(started_at);
            CREATE TABLE IF NOT EXISTS traffic_drop_trackers (
                route_key TEXT PRIMARY KEY,
                client_id INTEGER NOT NULL,
                client_name TEXT NOT NULL DEFAULT '',
                direction_name TEXT NOT NULL DEFAULT '',
                client_tech_prefix TEXT NOT NULL DEFAULT '',
                route_prefix TEXT NOT NULL DEFAULT '',
                voice_node TEXT NOT NULL DEFAULT 'primary',
                armed INTEGER NOT NULL DEFAULT 0,
                peak_calls INTEGER NOT NULL DEFAULT 0,
                last_active_calls INTEGER NOT NULL DEFAULT 0,
                armed_at TEXT,
                zero_started_at TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS traffic_drop_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_key TEXT NOT NULL,
                client_id INTEGER NOT NULL,
                client_name TEXT NOT NULL DEFAULT '',
                direction_name TEXT NOT NULL DEFAULT '',
                client_tech_prefix TEXT NOT NULL DEFAULT '',
                route_prefix TEXT NOT NULL DEFAULT '',
                voice_node TEXT NOT NULL DEFAULT 'primary',
                peak_calls INTEGER NOT NULL DEFAULT 0,
                silence_seconds INTEGER NOT NULL DEFAULT 0,
                zero_started_at TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                alert_sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_traffic_drop_alerts_pending
                ON traffic_drop_alerts(alert_sent_at, id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _cache_key(db, date_from, date_to):
    return (str(getattr(db, "DB_PATH", "default")), date_from, date_to)


def _parse_date(value, field_name):
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        raise HTTPException(422, f"{field_name}: укажите дату в формате YYYY-MM-DD")


def normalized_period(date_from=None, date_to=None):
    today = datetime.now(timezone.utc).date()
    start = _parse_date(date_from, "Дата с") if date_from else today
    end = _parse_date(date_to, "Дата по") if date_to else start
    if start > end:
        raise HTTPException(422, "Дата с не может быть позже даты по")
    if (end - start).days + 1 > _MAX_PERIOD_DAYS:
        raise HTTPException(422, f"Период не может быть больше {_MAX_PERIOD_DAYS} дней")
    return start, end


def period_snapshot(db, date_from=None, date_to=None, force=False):
    start, end = normalized_period(date_from, date_to)
    start_text = start.isoformat()
    end_text = end.isoformat()
    key = _cache_key(db, start_text, end_text)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if not force and cached and now - cached["stored_at"] < _CACHE_TTL_SECONDS:
            return cached["payload"]

    exclusive_end = (end + timedelta(days=1)).isoformat()
    conn = db.get_conn()
    try:
        rows = [dict(row) for row in conn.execute(
            """
            SELECT
                cd.client_id,
                COALESCE(NULLIF(c.name, ''), 'Без клиента') AS client_name,
                COALESCE(NULLIF(c.currency, ''), 'USD') AS currency,
                COALESCE(NULLIF(cd.terminator_destination_name, ''),
                         NULLIF(cd.destination, ''), 'Без направления') AS direction_name,
                COALESCE(NULLIF(cd.terminator_name, ''),
                         NULLIF(cd.gateway_name, ''), 'Без терминатора') AS terminator_name,
                COUNT(*) AS calls,
                SUM(CASE WHEN cd.billsec > 0 THEN 1 ELSE 0 END) AS connected_calls,
                COALESCE(SUM(cd.billsec), 0) AS billsec,
                COALESCE(SUM(cd.charged_cents), 0) AS revenue_units,
                COALESCE(SUM(cd.charged_cents - cd.margin_cents), 0) AS cost_units,
                COALESCE(SUM(cd.margin_cents), 0) AS margin_units,
                CASE WHEN SUM(CASE WHEN cd.billsec > 0 THEN cd.billsec ELSE 0 END) > 0
                     THEN CAST(ROUND(
                         1.0 * SUM(CASE WHEN cd.billsec > 0 THEN cd.sell_rate_cents * cd.billsec ELSE 0 END)
                         / SUM(CASE WHEN cd.billsec > 0 THEN cd.billsec ELSE 0 END)
                     ) AS INTEGER) ELSE 0 END AS average_sell_rate_units,
                CASE WHEN SUM(CASE WHEN cd.billsec > 0 THEN cd.billsec ELSE 0 END) > 0
                     THEN CAST(ROUND(
                         1.0 * SUM(CASE WHEN cd.billsec > 0 THEN cd.cost_rate_cents * cd.billsec ELSE 0 END)
                         / SUM(CASE WHEN cd.billsec > 0 THEN cd.billsec ELSE 0 END)
                     ) AS INTEGER) ELSE 0 END AS average_cost_rate_units
            FROM cdr cd
            LEFT JOIN clients c ON c.id = cd.client_id
            WHERE cd.started_at >= ? AND cd.started_at < ?
            GROUP BY cd.client_id, client_name, currency, direction_name, terminator_name
            HAVING calls > 0
            ORDER BY margin_units DESC, revenue_units DESC
            LIMIT 5000
            """,
            (start_text, exclusive_end),
        ).fetchall()]
    finally:
        conn.close()

    payload = {
        "money_scale": db.MONEY_SCALE,
        "timezone": "UTC",
        "date_from": start_text,
        "date_to": end_text,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        "summary": {
            "calls": sum(int(row["calls"] or 0) for row in rows),
            "connected_calls": sum(int(row["connected_calls"] or 0) for row in rows),
            "billsec": sum(int(row["billsec"] or 0) for row in rows),
            "revenue_units": sum(int(row["revenue_units"] or 0) for row in rows),
            "cost_units": sum(int(row["cost_units"] or 0) for row in rows),
            "margin_units": sum(int(row["margin_units"] or 0) for row in rows),
        },
        "rows": rows,
    }
    with _cache_lock:
        _cache[key] = {"stored_at": now, "payload": payload}
    return payload


def daily_snapshot(db, force=False):
    return period_snapshot(db, force=force)


def _utc_text(timestamp):
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _route_key(row):
    return "|".join(
        (
            str(int(row["client_id"])),
            str(row["client_tech_prefix"] or ""),
            str(row["route_prefix"] or ""),
            voice_nodes.normalize(row["voice_node"]),
        )
    )


def _current_route_calls(conn, now_ts):
    rows = conn.execute(
        """
        SELECT
            cr.client_id,
            c.name AS client_name,
            COALESCE(cr.destination_name, '') AS direction_name,
            COALESCE(cr.client_tech_prefix, '') AS client_tech_prefix,
            COALESCE(cr.prefix, '') AS route_prefix,
            COALESCE(NULLIF(cr.voice_node, ''), 'primary') AS voice_node,
            COUNT(r.id) AS active_calls
        FROM client_rates cr
        JOIN clients c ON c.id = cr.client_id
        LEFT JOIN reservations r
          ON r.client_rate_id = cr.id AND r.expires_at > ?
        WHERE c.active = 1
        GROUP BY cr.client_id, c.name, direction_name, client_tech_prefix,
                 route_prefix, voice_node
        """,
        (int(now_ts),),
    ).fetchall()
    current = {}
    for row in rows:
        item = dict(row)
        item["voice_node"] = voice_nodes.normalize(item["voice_node"])
        item["route_key"] = _route_key(item)
        current[item["route_key"]] = item
    return current


def evaluate_traffic_drop_alerts(db, now_ts=None, threshold_calls=None, silence_seconds=None):
    now_ts = int(time.time() if now_ts is None else now_ts)
    settings = traffic_drop_settings()
    threshold_calls = max(1, int(threshold_calls or settings["threshold_calls"]))
    silence_seconds = max(1, int(silence_seconds or settings["silence_seconds"]))
    now_text = _utc_text(now_ts)

    conn = db.get_conn()
    created = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _current_route_calls(conn, now_ts)
        trackers = {
            row["route_key"]: dict(row)
            for row in conn.execute("SELECT * FROM traffic_drop_trackers").fetchall()
        }

        for route_key, route in current.items():
            active = int(route["active_calls"] or 0)
            tracker = trackers.get(route_key)
            if tracker is None:
                conn.execute(
                    """
                    INSERT INTO traffic_drop_trackers
                        (route_key, client_id, client_name, direction_name,
                         client_tech_prefix, route_prefix, voice_node,
                         armed, peak_calls, last_active_calls, armed_at,
                         zero_started_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        route_key, route["client_id"], route["client_name"],
                        route["direction_name"], route["client_tech_prefix"],
                        route["route_prefix"], route["voice_node"],
                        int(active >= threshold_calls), active if active >= threshold_calls else 0,
                        active, now_text if active >= threshold_calls else None, None, now_text,
                    ),
                )
                continue

            armed = bool(tracker["armed"])
            peak = int(tracker["peak_calls"] or 0)
            zero_started_at = tracker["zero_started_at"]
            armed_at = tracker["armed_at"]

            if active >= threshold_calls:
                armed = True
                peak = max(peak, active)
                zero_started_at = None
                armed_at = armed_at or now_text
            elif armed and active > 0:
                zero_started_at = None
            elif armed and not zero_started_at:
                zero_started_at = now_text
            elif armed and zero_started_at:
                zero_start = datetime.strptime(zero_started_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                silent_for = max(0, now_ts - int(zero_start.timestamp()))
                if silent_for >= silence_seconds:
                    cursor = conn.execute(
                        """
                        INSERT INTO traffic_drop_alerts
                            (route_key, client_id, client_name, direction_name,
                             client_tech_prefix, route_prefix, voice_node, peak_calls,
                             silence_seconds, zero_started_at, detected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            route_key, route["client_id"], route["client_name"],
                            route["direction_name"], route["client_tech_prefix"],
                            route["route_prefix"], route["voice_node"], peak,
                            silent_for, zero_started_at, now_text,
                        ),
                    )
                    created.append(int(cursor.lastrowid))
                    armed = False
                    peak = 0
                    armed_at = None
                    zero_started_at = None

            conn.execute(
                """
                UPDATE traffic_drop_trackers
                   SET client_name = ?, direction_name = ?, client_tech_prefix = ?,
                       route_prefix = ?, voice_node = ?, armed = ?, peak_calls = ?,
                       last_active_calls = ?, armed_at = ?, zero_started_at = ?,
                       updated_at = ?
                 WHERE route_key = ?
                """,
                (
                    route["client_name"], route["direction_name"],
                    route["client_tech_prefix"], route["route_prefix"],
                    route["voice_node"], int(armed), peak, active, armed_at,
                    zero_started_at, now_text, route_key,
                ),
            )

        conn.execute(
            "DELETE FROM traffic_drop_trackers "
            "WHERE armed = 0 AND updated_at < datetime(?, '-7 days')",
            (now_text,),
        )
        conn.commit()
        return {"ok": True, "created_alert_ids": created}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pending_traffic_drop_alerts(db, evaluate=True):
    if evaluate:
        evaluate_traffic_drop_alerts(db)
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM traffic_drop_alerts WHERE alert_sent_at IS NULL "
            "ORDER BY id LIMIT 100"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_traffic_drop_alert_notified(db, alert_id):
    conn = db.get_conn()
    try:
        cursor = conn.execute(
            "UPDATE traffic_drop_alerts SET alert_sent_at = datetime('now') "
            "WHERE id = ? AND alert_sent_at IS NULL",
            (int(alert_id),),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "Traffic drop alert не найден или уже отправлен")
        return {"ok": True}
    finally:
        conn.close()


def install(app, main, db, base_path: Path):
    init_schema(db)

    @app.on_event("startup")
    def _monitoring_startup():
        init_schema(db)

    @app.get("/monitoring", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def monitoring_page(request: Request):
        html = (base_path / "monitoring.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/monitoring/daily", dependencies=main.ADMIN_AUTH)
    def monitoring_daily(date_from: str = "", date_to: str = "", refresh: bool = False):
        return period_snapshot(db, date_from or None, date_to or None, force=refresh)

    @app.get("/api/ops/traffic-drop-alerts/pending", dependencies=main.API_AUTH)
    def traffic_drop_alerts_pending():
        settings = traffic_drop_settings()
        return {"alerts": pending_traffic_drop_alerts(db), **settings}

    @app.post("/api/ops/traffic-drop-alerts/{alert_id}/notified", dependencies=main.API_AUTH)
    def traffic_drop_alert_notified(alert_id: int, notification: TrafficDropNotificationIn):
        if notification.kind != "alert":
            raise HTTPException(422, "Unsupported notification kind")
        return mark_traffic_drop_alert_notified(db, alert_id)
