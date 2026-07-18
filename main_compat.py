from pathlib import Path
import math

from fastapi import HTTPException, Request, status
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


_remove_routes({"/", "/api/dashboard-data", "/api/firewall-whitelist", "/api/finalize"})


@app.get("/api/firewall-whitelist", dependencies=main.API_AUTH)
def firewall_whitelist():
    conn = db.get_conn()
    try:
        entries = []
        seen = set()

        def add_entries(raw_ips, **meta):
            for token in db.split_ip_list(raw_ips):
                if token in seen:
                    continue
                seen.add(token)
                entries.append({"ip": token, **meta})

        clients = conn.execute(
            "SELECT id, name, sip_ip FROM clients WHERE active = 1 ORDER BY id"
        ).fetchall()
        for row in clients:
            add_entries(row["sip_ip"], source="client", client_id=row["id"], client_name=row["name"])

        groups = conn.execute(
            "SELECT id, name, ips FROM termination_groups WHERE active = 1 ORDER BY id"
        ).fetchall()
        for row in groups:
            add_entries(row["ips"], source="termination_group", group_id=row["id"], group_name=row["name"])

        terminators = conn.execute(
            "SELECT t.id, t.name, t.ips, t.gateway_group_id, g.name AS group_name, g.ips AS group_ips "
            "FROM terminators t "
            "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
            "WHERE t.active = 1 ORDER BY t.id"
        ).fetchall()
        for row in terminators:
            add_entries(row["ips"], source="terminator", terminator_id=row["id"], terminator_name=row["name"])
            add_entries(
                row["group_ips"],
                source="terminator_group",
                terminator_id=row["id"],
                terminator_name=row["name"],
                group_id=row["gateway_group_id"],
                group_name=row["group_name"],
            )

        return {"ok": True, "entries": entries}
    finally:
        conn.close()


@app.post("/api/finalize", dependencies=main.API_AUTH)
def finalize(data: main.FinalizeIn):
    conn = db.get_conn()
    try:
        bsec = db.billed_seconds(data.billsec)
        charged = math.ceil(bsec * data.sell_rate_cents / 60)
        cost = math.ceil(bsec * data.cost_rate_cents / 60)
        conn.execute("BEGIN IMMEDIATE")
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (data.client_id,)).fetchone()
        if client is None:
            raise HTTPException(404, "Клиент не найден")

        new_balance = client["balance_cents"] - charged
        if new_balance < 0:
            print(
                f"[FINALIZE WARN] client={data.client_id} call={data.call_uuid} "
                f"charged={charged} > balance={client['balance_cents']}: clamp to 0"
            )
            charged = client["balance_cents"]
            new_balance = 0

        margin = charged - cost
        conn.execute("UPDATE clients SET balance_cents = ? WHERE id = ?", (new_balance, data.client_id))
        conn.execute(
            "INSERT INTO cdr (client_id, call_uuid, sip_ip, clid, destination, client_tech_prefix, "
            "dial_destination, provider_number, gateway_name, route_ip, terminator_id, terminator_name, "
            "terminator_destination_name, terminator_prefix, terminator_tech_prefix, hangup_cause, "
            "bridge_hangup_cause, result, billsec, sell_rate_cents, cost_rate_cents, charged_cents, "
            "margin_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.client_id,
                data.call_uuid,
                data.sip_ip,
                data.clid,
                data.destination,
                data.client_tech_prefix,
                data.dial_destination,
                data.provider_number,
                data.gateway_name,
                data.route_ip,
                data.terminator_id,
                data.terminator_name,
                data.terminator_destination_name,
                data.terminator_prefix,
                data.terminator_tech_prefix,
                data.hangup_cause,
                data.bridge_hangup_cause,
                data.result,
                data.billsec,
                data.sell_rate_cents,
                data.cost_rate_cents,
                charged,
                margin,
            ),
        )

        final_status = "answered" if data.billsec > 0 else "failed"
        hangup = data.bridge_hangup_cause or data.hangup_cause or data.result or ""
        reason_parts = [part for part in (hangup, data.result if data.result != hangup else "") if part]
        reason_parts.append(f"billsec={data.billsec}")
        if charged:
            reason_parts.append(f"charged={charged}")
        if margin:
            reason_parts.append(f"margin={margin}")
        conn.execute(
            "UPDATE sip_hits SET status = ?, stage = ?, reason = ?, client_id = ?, client_name = ?, "
            "client_tech_prefix = ?, dial_destination = ?, provider_number = ?, gateway_name = ?, "
            "route_ip = ?, terminator_id = ?, terminator_name = ?, terminator_destination_name = ?, "
            "terminator_prefix = ?, sell_rate_cents = ?, cost_rate_cents = ? WHERE call_uuid = ?",
            (
                final_status,
                "finalized",
                " · ".join(reason_parts),
                data.client_id,
                client["name"],
                data.client_tech_prefix,
                data.dial_destination,
                data.provider_number,
                data.gateway_name or "",
                data.route_ip,
                data.terminator_id,
                data.terminator_name,
                data.terminator_destination_name,
                data.terminator_prefix,
                data.sell_rate_cents,
                data.cost_rate_cents,
                data.call_uuid,
            ),
        )

        conn.execute("DELETE FROM reservations WHERE call_uuid = ?", (data.call_uuid,))
        conn.commit()
        return {"ok": True, "charged_cents": charged, "margin_cents": margin, "balance_cents": new_balance}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


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
