from pathlib import Path

from fastapi import Request, status
from fastapi.responses import HTMLResponse, JSONResponse

import db
import main


app = main.app


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _uses_current_money_scale(request: Request) -> bool:
    return request.headers.get("x-money-scale", "").strip() == str(db.MONEY_SCALE)


def _legacy_money(value):
    if value is None:
        return value
    return value / db.LEGACY_CENT_TO_MONEY_UNITS


def _legacy_rows(rows, fields):
    out = []
    for row in rows:
        item = dict(row)
        for field in fields:
            if field in item:
                item[field] = _legacy_money(item[field])
        out.append(item)
    return out


def _rows(rows):
    return [dict(row) for row in rows]


def _remove_routes(paths):
    app.router.routes = [
        route for route in app.router.routes
        if getattr(route, "path", "") not in paths
    ]


@app.middleware("http")
async def dashboard_scale_guard(request: Request, call_next):
    admin_write = (
        request.url.path.startswith("/api/")
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and not request.url.path.startswith((
            "/api/reserve",
            "/api/finalize",
            "/api/sip-guard",
            "/api/firewall-whitelist",
            "/api/ops/",
        ))
    )
    if admin_write and request.headers.get("authorization") and not _uses_current_money_scale(request):
        response = JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "Страница биллинга устарела. Обновите вкладку и повторите действие."},
        )
        return _no_store(response)

    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        _no_store(response)
    return response


_remove_routes({"/", "/api/dashboard-data"})


@app.get("/api/dashboard-data", dependencies=main.ADMIN_AUTH)
def dashboard_data(request: Request):
    conn = db.get_conn()
    try:
        clients = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
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
            "LEFT JOIN terminators t ON t.id = cr.terminator_id ORDER BY cr.client_id"
        ).fetchall()
        cdr = conn.execute(
            "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
            "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
            "ORDER BY cd.id DESC LIMIT 10"
        ).fetchall()
        sip_hits = conn.execute(
            "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
            "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT 50"
        ).fetchall()
        e164_directions = db.list_e164_countries(conn)
        total_balance = conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM clients").fetchone()["s"]
        margin_today = conn.execute("SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE date(started_at)=date('now')").fetchone()["s"]
        margin_month = conn.execute("SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE strftime('%Y-%m', started_at)=strftime('%Y-%m','now')").fetchone()["s"]
    finally:
        conn.close()

    if not _uses_current_money_scale(request):
        return {
            "money_scale": 100,
            "clients": _legacy_rows(clients, ("balance_cents",)),
            "termination_groups": _rows(groups),
            "terminators": _legacy_rows(terminators, ("cost_rate_cents",)),
            "client_rates": _legacy_rows(client_rates, ("sell_rate_cents",)),
            "cdr": _legacy_rows(cdr, ("sell_rate_cents", "cost_rate_cents", "charged_cents", "margin_cents")),
            "sip_hits": _legacy_rows(sip_hits, ("sell_rate_cents", "cost_rate_cents")),
            "e164_directions": e164_directions,
            "summary": {
                "total_balance_cents": _legacy_money(total_balance),
                "margin_today_cents": _legacy_money(margin_today),
                "margin_month_cents": _legacy_money(margin_month),
            },
        }

    return {
        "money_scale": db.MONEY_SCALE,
        "clients": _rows(clients),
        "termination_groups": _rows(groups),
        "terminators": _rows(terminators),
        "client_rates": _rows(client_rates),
        "cdr": _rows(cdr),
        "sip_hits": _rows(sip_hits),
        "e164_directions": e164_directions,
        "summary": {
            "total_balance_cents": total_balance,
            "margin_today_cents": margin_today,
            "margin_month_cents": margin_month,
        },
    }


@app.get("/", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
def dashboard(request: Request):
    html = Path("dashboard.html").read_text(encoding="utf-8")
    html = html.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">\n'
        '<meta http-equiv="Pragma" content="no-cache">\n'
        '<meta http-equiv="Expires" content="0">',
    )
    html = html.replace(
        "headers: {'Content-Type':'application/json'}",
        "headers: {'Content-Type':'application/json', 'X-Money-Scale': String(MONEY_SCALE)}",
    )
    html = html.replace(
        "fetch('/api/dashboard-data', {cache:'no-store'})",
        "fetch('/api/dashboard-data', {cache:'no-store', headers: {'X-Money-Scale': String(MONEY_SCALE)}})",
    )
    return _no_store(HTMLResponse(html))
