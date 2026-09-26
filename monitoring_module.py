"""Low-overhead daily profitability monitoring for finalized CDRs."""

from pathlib import Path
import threading
import time

from fastapi import Request
from fastapi.responses import HTMLResponse


_CACHE_TTL_SECONDS = 60
_cache_lock = threading.Lock()
_cache = {}


def init_schema(db):
    conn = db.get_conn()
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cdr_started ON cdr(started_at)")
        conn.commit()
    finally:
        conn.close()


def _cache_key(db):
    return str(getattr(db, "DB_PATH", "default"))


def daily_snapshot(db, force=False):
    key = _cache_key(db)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if not force and cached and now - cached["stored_at"] < _CACHE_TTL_SECONDS:
            return cached["payload"]

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
            WHERE cd.started_at >= datetime('now', 'start of day')
              AND cd.started_at < datetime('now', 'start of day', '+1 day')
            GROUP BY cd.client_id, client_name, currency, direction_name, terminator_name
            HAVING revenue_units <> 0 OR margin_units <> 0
            ORDER BY margin_units DESC, revenue_units DESC
            LIMIT 1000
            """
        ).fetchall()]
    finally:
        conn.close()

    payload = {
        "money_scale": db.MONEY_SCALE,
        "timezone": "UTC",
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
    def monitoring_daily(refresh: bool = False):
        return daily_snapshot(db, force=refresh)
