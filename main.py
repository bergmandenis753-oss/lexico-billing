"""
main.py — FastAPI биллинг.

Точки для FreeSWITCH:
    POST /api/reserve   — перед звонком (проверка + холд, вернёт gateway и max_seconds)
    POST /api/finalize  — после звонка (списание + CDR)
"""

import math
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import db

app = FastAPI(title="Lexico VoIP billing")
templates = Jinja2Templates(directory=".")


@app.on_event("startup")
def _startup():
    db.init_db()


# ---------------- схемы входа ----------------

class ReserveIn(BaseModel):
    sip_ip: str
    destination: str
    call_uuid: Optional[str] = None


class FinalizeIn(BaseModel):
    client_id: int
    call_uuid: str
    destination: str
    billsec: int = Field(ge=0)
    sell_rate_cents: int = Field(ge=0)
    cost_rate_cents: int = Field(ge=0)
    gateway_name: Optional[str] = None


class ClientIn(BaseModel):
    name: str
    sip_ip: str
    currency: str = "USD"
    balance_cents: int = 0
    active: bool = True


class ClientUpdateIn(BaseModel):
    name: Optional[str] = None
    sip_ip: Optional[str] = None
    currency: Optional[str] = None
    active: Optional[bool] = None


class TopupIn(BaseModel):
    amount_cents: int = Field(gt=0)


class RouteIn(BaseModel):
    destination_name: str
    prefix: str
    gateway_name: str
    cost_rate_cents: int = Field(ge=0)
    active: bool = True


class RouteUpdateIn(BaseModel):
    destination_name: Optional[str] = None
    prefix: Optional[str] = None
    gateway_name: Optional[str] = None
    cost_rate_cents: Optional[int] = None
    active: Optional[bool] = None


class ClientRateIn(BaseModel):
    client_id: int
    prefix: str
    destination_name: str
    sell_rate_cents: int = Field(ge=0)


class ClientRateUpdateIn(BaseModel):
    prefix: Optional[str] = None
    destination_name: Optional[str] = None
    sell_rate_cents: Optional[int] = None


# ================= БИЛЛИНГ =================

@app.post("/api/reserve")
def reserve(data: ReserveIn):
    conn = db.get_conn()
    try:
        now_ts = db.now()
        conn.execute("BEGIN IMMEDIATE")
        db.cleanup_expired(conn, now_ts)

        client = db.get_client_by_ip(conn, data.sip_ip)
        if client is None:
            raise HTTPException(403, f"Клиент с IP {data.sip_ip} не найден")
        if not client["active"]:
            raise HTTPException(403, "Клиент неактивен")

        rate = db.match_client_rate(conn, client["id"], data.destination)
        if rate is None:
            raise HTTPException(403, f"Нет тарифа клиента для {data.destination}")
        if rate["sell_rate_cents"] <= 0:
            raise HTTPException(403, "Некорректный тариф продажи (<= 0)")

        route = db.match_active_route(conn, data.destination)
        if route is None:
            raise HTTPException(403, f"Нет активного роута для {data.destination}")

        held = db.active_hold_sum(conn, client["id"], now_ts)
        available = client["balance_cents"] - held
        if available <= 0:
            raise HTTPException(403, "Недостаточно средств (баланс занят активными звонками)")

        max_seconds = math.floor(available / rate["sell_rate_cents"] * 60)
        if max_seconds <= 0:
            raise HTTPException(403, "Недостаточно средств на минуту разговора")

        call_uuid = data.call_uuid or f"nouuid-{now_ts}-{client['id']}"
        expires_at = now_ts + max_seconds + db.RESERVATION_BUFFER_SEC
        conn.execute(
            "INSERT OR REPLACE INTO reservations (client_id, call_uuid, reserved_cents, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (client["id"], call_uuid, available, expires_at),
        )
        conn.commit()

        return {
            "allowed": True,
            "max_seconds": max_seconds,
            "sell_rate_cents": rate["sell_rate_cents"],
            "cost_rate_cents": route["cost_rate_cents"],
            "gateway_name": route["gateway_name"],
            "client_id": client["id"],
            "call_uuid": call_uuid,
        }
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/finalize")
def finalize(data: FinalizeIn):
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
            print(f"[FINALIZE WARN] client={data.client_id} call={data.call_uuid} "
                  f"charged={charged} > balance={client['balance_cents']}: clamp to 0")
            charged = client["balance_cents"]
            new_balance = 0

        margin = charged - cost  # честная маржа, даже при clamp'е

        conn.execute("UPDATE clients SET balance_cents = ? WHERE id = ?", (new_balance, data.client_id))
        conn.execute(
            "INSERT INTO cdr (client_id, call_uuid, destination, gateway_name, billsec, "
            "sell_rate_cents, cost_rate_cents, charged_cents, margin_cents) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data.client_id, data.call_uuid, data.destination, data.gateway_name, data.billsec,
             data.sell_rate_cents, data.cost_rate_cents, charged, margin),
        )
        conn.execute("DELETE FROM reservations WHERE call_uuid = ?", (data.call_uuid,))
        conn.commit()
        return {"ok": True, "charged_cents": charged, "margin_cents": margin, "balance_cents": new_balance}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ================= CRUD клиентов =================

@app.post("/api/clients")
def create_client(data: ClientIn):
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO clients (name, sip_ip, balance_cents, currency, active) VALUES (?, ?, ?, ?, ?)",
            (data.name, data.sip_ip, data.balance_cents, data.currency, int(data.active)),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    except db.sqlite3.IntegrityError:
        raise HTTPException(409, f"IP {data.sip_ip} уже используется")
    finally:
        conn.close()


@app.get("/api/clients")
def list_clients():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/clients/{cid}")
def update_client(cid: int, data: ClientUpdateIn):
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    if "active" in fields:
        fields["active"] = int(fields["active"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = db.get_conn()
    try:
        cur = conn.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*fields.values(), cid))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Клиент не найден")
        return {"ok": True}
    except db.sqlite3.IntegrityError:
        raise HTTPException(409, "IP уже используется")
    finally:
        conn.close()


@app.post("/api/clients/{cid}/topup")
def topup(cid: int, data: TopupIn):
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE clients SET balance_cents = balance_cents + ? WHERE id = ?",
            (data.amount_cents, cid),
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise HTTPException(404, "Клиент не найден")
        row = conn.execute("SELECT balance_cents FROM clients WHERE id = ?", (cid,)).fetchone()
        conn.commit()
        return {"ok": True, "balance_cents": row["balance_cents"]}
    finally:
        conn.close()


# ================= CRUD роутов =================

@app.post("/api/routes")
def create_route(data: RouteIn):
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # если новый роут активен — гасим другие активные на этот же префикс
        if data.active:
            conn.execute("UPDATE routes SET active = 0 WHERE prefix = ?", (data.prefix,))
        cur = conn.execute(
            "INSERT INTO routes (destination_name, prefix, gateway_name, cost_rate_cents, active) "
            "VALUES (?, ?, ?, ?, ?)",
            (data.destination_name, data.prefix, data.gateway_name, data.cost_rate_cents, int(data.active)),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/api/routes")
def list_routes():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM routes ORDER BY prefix, active DESC, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/routes/{rid}")
def update_route(rid: int, data: RouteUpdateIn):
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM routes WHERE id = ?", (rid,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(404, "Роут не найден")
        # активируем этот роут — гасим соседей по префиксу
        if fields.get("active"):
            prefix = fields.get("prefix", row["prefix"])
            conn.execute("UPDATE routes SET active = 0 WHERE prefix = ? AND id != ?", (prefix, rid))
        if "active" in fields:
            fields["active"] = int(fields["active"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE routes SET {sets} WHERE id = ?", (*fields.values(), rid))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/routes/{rid}/activate")
def activate_route(rid: int):
    """Сделать роут активным (остальные на этот префикс — в резерв)."""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM routes WHERE id = ?", (rid,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(404, "Роут не найден")
        conn.execute("UPDATE routes SET active = 0 WHERE prefix = ?", (row["prefix"],))
        conn.execute("UPDATE routes SET active = 1 WHERE id = ?", (rid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/routes/{rid}")
def delete_route(rid: int):
    conn = db.get_conn()
    try:
        cur = conn.execute("DELETE FROM routes WHERE id = ?", (rid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Роут не найден")
        return {"ok": True}
    finally:
        conn.close()


# ================= CRUD тарифов клиентов =================

@app.post("/api/client-rates")
def create_client_rate(data: ClientRateIn):
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM clients WHERE id = ?", (data.client_id,)).fetchone()
        if exists is None:
            raise HTTPException(404, "Клиент не найден")
        cur = conn.execute(
            "INSERT INTO client_rates (client_id, prefix, destination_name, sell_rate_cents) "
            "VALUES (?, ?, ?, ?)",
            (data.client_id, data.prefix, data.destination_name, data.sell_rate_cents),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/api/client-rates")
def list_client_rates():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT cr.*, c.name AS client_name FROM client_rates cr "
        "JOIN clients c ON c.id = cr.client_id ORDER BY cr.client_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/client-rates/{rid}")
def update_client_rate(rid: int, data: ClientRateUpdateIn):
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = db.get_conn()
    try:
        cur = conn.execute(f"UPDATE client_rates SET {sets} WHERE id = ?", (*fields.values(), rid))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Тариф не найден")
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/client-rates/{rid}")
def delete_client_rate(rid: int):
    conn = db.get_conn()
    try:
        cur = conn.execute("DELETE FROM client_rates WHERE id = ?", (rid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Тариф не найден")
        return {"ok": True}
    finally:
        conn.close()


# ================= Дашборд =================

@app.get("/api/dashboard-data")
def dashboard_data():
    conn = db.get_conn()

    clients = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
    routes = conn.execute("SELECT * FROM routes ORDER BY prefix, active DESC, id").fetchall()
    client_rates = conn.execute(
        "SELECT cr.*, c.name AS client_name FROM client_rates cr "
        "JOIN clients c ON c.id = cr.client_id ORDER BY cr.client_id"
    ).fetchall()
    cdr = conn.execute("SELECT * FROM cdr ORDER BY id DESC LIMIT 50").fetchall()

    total_balance = conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM clients").fetchone()["s"]
    margin_today = conn.execute(
        "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE date(started_at)=date('now')"
    ).fetchone()["s"]
    margin_month = conn.execute(
        "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr "
        "WHERE strftime('%Y-%m', started_at)=strftime('%Y-%m','now')"
    ).fetchone()["s"]

    conn.close()
    return {
        "clients": [dict(r) for r in clients],
        "routes": [dict(r) for r in routes],
        "client_rates": [dict(r) for r in client_rates],
        "cdr": [dict(r) for r in cdr],
        "summary": {
            "total_balance_cents": total_balance,
            "margin_today_cents": margin_today,
            "margin_month_cents": margin_month,
        },
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})
