import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse


MANUAL_MARGIN_ADJUSTMENT = 663900
MANUAL_MARGIN_DAY = "2026-07-24"
MANUAL_MARGIN_MONTH = "2026-07"


def _remove_routes(app, path, methods=None):
    wanted = {m.upper() for m in methods} if methods else None
    app.router.routes = [
        route for route in app.router.routes
        if not (
            getattr(route, "path", "") == path
            and (wanted is None or set(getattr(route, "methods", set()) or set()) & wanted)
        )
    ]


def _rows(rows):
    return [dict(row) for row in rows]


def _ensure_deleted_at(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()]
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN deleted_at TEXT")
        conn.commit()


def _money(value, db, currency="USD"):
    return f"{(int(value or 0) / db.MONEY_SCALE):.4f} {currency or 'USD'}"


def _format_reason(reason, db, currency="USD"):
    text = str(reason or "")
    text = re.sub(r"\bcharged=(\d+)\b", lambda m: f"списано={_money(m.group(1), db, currency)}", text)
    text = re.sub(r"\bmargin=(\-?\d+)\b", lambda m: f"маржа={_money(m.group(1), db, currency)}", text)
    text = text.replace("billsec=", "сек=")
    return text


def _apply_manual_margin_adjustment(conn, margin_today, margin_month):
    today = conn.execute("SELECT date('now') AS d").fetchone()["d"]
    month = conn.execute("SELECT strftime('%Y-%m','now') AS m").fetchone()["m"]
    if today == MANUAL_MARGIN_DAY:
        margin_today -= MANUAL_MARGIN_ADJUSTMENT
    if month == MANUAL_MARGIN_MONTH:
        margin_month -= MANUAL_MARGIN_ADJUSTMENT
    return margin_today, margin_month


def install(app, main, db):
    _remove_routes(app, "/", {"GET"})
    _remove_routes(app, "/api/dashboard-data", {"GET"})

    @app.get("/api/dashboard-data", dependencies=main.ADMIN_AUTH)
    def dashboard_data():
        conn = db.get_conn()
        try:
            _ensure_deleted_at(conn)
            clients = conn.execute("SELECT * FROM clients WHERE deleted_at IS NULL ORDER BY id").fetchall()
            groups = conn.execute("SELECT * FROM termination_groups ORDER BY name").fetchall()
            terminators = conn.execute(
                "SELECT t.*, g.name AS gateway_group_name, g.ips AS gateway_group_ips, "
                "g.gateway_name AS gateway_group_gateway_name FROM terminators t "
                "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
                "ORDER BY t.prefix, t.active DESC, t.id"
            ).fetchall()
            client_rates = conn.execute(
                "SELECT cr.*, c.name AS client_name, t.name AS terminator_name "
                "FROM client_rates cr JOIN clients c ON c.id = cr.client_id "
                "LEFT JOIN terminators t ON t.id = cr.terminator_id "
                "WHERE c.deleted_at IS NULL ORDER BY cr.client_id"
            ).fetchall()
            cdr = conn.execute(
                "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
                "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
                "ORDER BY cd.id DESC LIMIT 10"
            ).fetchall()
            sip_hits_rows = conn.execute(
                "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
                "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT 50"
            ).fetchall()
            sip_hits = []
            for row in sip_hits_rows:
                item = dict(row)
                item["reason"] = _format_reason(item.get("reason"), db, item.get("client_currency") or "USD")
                sip_hits.append(item)
            e164_directions = db.list_e164_countries(conn)
            total_balance = conn.execute(
                "SELECT COALESCE(SUM(balance_cents),0) AS s FROM clients WHERE deleted_at IS NULL"
            ).fetchone()["s"]
            margin_today = conn.execute(
                "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE date(started_at)=date('now')"
            ).fetchone()["s"]
            margin_month = conn.execute(
                "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE strftime('%Y-%m', started_at)=strftime('%Y-%m','now')"
            ).fetchone()["s"]
            margin_today, margin_month = _apply_manual_margin_adjustment(conn, margin_today, margin_month)
            return {
                "money_scale": db.MONEY_SCALE,
                "clients": _rows(clients),
                "termination_groups": _rows(groups),
                "terminators": _rows(terminators),
                "client_rates": _rows(client_rates),
                "cdr": _rows(cdr),
                "sip_hits": sip_hits,
                "e164_directions": e164_directions,
                "summary": {
                    "total_balance_cents": total_balance,
                    "margin_today_cents": margin_today,
                    "margin_month_cents": margin_month,
                },
            }
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def dashboard(request: Request):
        html = Path("dashboard.html").read_text(encoding="utf-8")
        html = html.replace(
            "const ok = x.status === 'allowed';",
            "const ok = ['allowed','answered','finalized'].includes(String(x.status || '').toLowerCase());",
        )
        html = html.replace(
            "method, headers: {'Content-Type':'application/json'},",
            "method, headers: {'Content-Type':'application/json', 'X-Money-Scale': String(MONEY_SCALE)},",
        )
        html = html.replace(
            "fetch('/api/dashboard-data', {cache:'no-store'})",
            "fetch('/api/dashboard-data', {cache:'no-store', headers: {'X-Money-Scale': String(MONEY_SCALE)}})",
        )
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })