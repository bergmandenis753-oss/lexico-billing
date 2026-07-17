"""
main.py — FastAPI биллинг.

Точки для FreeSWITCH:
    POST /api/reserve   — перед звонком (проверка + холд, вернёт gateway и max_seconds)
    POST /api/finalize  — после звонка (списание + CDR)
"""

import math
import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import db

app = FastAPI(
    title="Lexico VoIP billing",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory=".")
basic_security = HTTPBasic(auto_error=False)


def _auth_not_configured(detail: str):
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _admin_unauthorized():
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


def require_admin(credentials: Optional[HTTPBasicCredentials] = Depends(basic_security)):
    admin_user = os.getenv("ADMIN_USER")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_user or not admin_password:
        _auth_not_configured("Admin credentials are not configured")
    if credentials is None:
        _admin_unauthorized()

    user_ok = secrets.compare_digest(credentials.username, admin_user)
    password_ok = secrets.compare_digest(credentials.password, admin_password)
    if not (user_ok and password_ok):
        _admin_unauthorized()
    return credentials.username


def require_api_key(request: Request):
    expected = os.getenv("API_SECRET_KEY")
    if not expected:
        _auth_not_configured("API secret is not configured")

    token = request.headers.get("x-api-key", "")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return True


ADMIN_AUTH = [Depends(require_admin)]
API_AUTH = [Depends(require_api_key)]


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


class TerminationGroupIn(BaseModel):
    name: str
    ips: str = ""
    gateway_name: str = ""
    active: bool = True


class TerminationGroupUpdateIn(BaseModel):
    name: Optional[str] = None
    ips: Optional[str] = None
    gateway_name: Optional[str] = None
    active: Optional[bool] = None


class TerminatorIn(BaseModel):
    name: str                  # 'Lexico'
    gateway_group_id: Optional[int] = None
    ips: str = ""              # IP поставщика через запятую
    destination_name: str
    prefix: str
    gateway_name: str = ""     # если пусто — FreeSWITCH шлёт напрямую на IP
    tech_prefix: str = ""      # техпрефикс поставщика (напр. '999001')
    cost_rate_cents: int = Field(ge=0)
    active: bool = True


class TerminatorUpdateIn(BaseModel):
    name: Optional[str] = None
    gateway_group_id: Optional[int] = None
    ips: Optional[str] = None
    destination_name: Optional[str] = None
    prefix: Optional[str] = None
    gateway_name: Optional[str] = None
    tech_prefix: Optional[str] = None
    cost_rate_cents: Optional[int] = None
    active: Optional[bool] = None


class ClientRateIn(BaseModel):
    client_id: int
    terminator_id: Optional[int] = None   # персональный терминатор оригинатора
    prefix: str
    destination_name: str
    sell_rate_cents: int = Field(ge=0)


class ClientRateUpdateIn(BaseModel):
    prefix: Optional[str] = None
    destination_name: Optional[str] = None
    sell_rate_cents: Optional[int] = None


# ================= БИЛЛИНГ =================

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.post("/api/reserve", dependencies=API_AUTH)
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

        # Персональный роут: сначала терминатор, назначенный этому оригинатору
        # в его роуте; если не задан — глобально активный на этот префикс.
        route = db.get_terminator(conn, rate["terminator_id"]) if "terminator_id" in rate.keys() else None
        if route is None:
            route = db.match_active_terminator(conn, data.destination)
        if route is None:
            raise HTTPException(403, f"Нет терминатора для {data.destination}")
        group = None
        if "gateway_group_id" in route.keys():
            group = db.get_termination_group(conn, route["gateway_group_id"])
        gateway_name = (route["gateway_name"] or "").strip()
        route_ips = route["ips"] or ""
        if group is not None:
            gateway_name = gateway_name or (group["gateway_name"] or "").strip()
            route_ips = route_ips or group["ips"] or ""
        route_ip = db.pick_ip(route_ips, data.call_uuid or data.destination)
        if not gateway_name and not route_ip:
            raise HTTPException(403, "У терминатора не указан ни gateway, ни IP")

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
            "gateway_name": gateway_name,
            "route_ip": route_ip,
            "tech_prefix": route["tech_prefix"],
            "client_id": client["id"],
            "call_uuid": call_uuid,
        }
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/finalize", dependencies=API_AUTH)
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

@app.post("/api/clients", dependencies=ADMIN_AUTH)
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


@app.get("/api/clients", dependencies=ADMIN_AUTH)
def list_clients():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/clients/{cid}", dependencies=ADMIN_AUTH)
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


@app.post("/api/clients/{cid}/topup", dependencies=ADMIN_AUTH)
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


# ================= CRUD терминационных групп =================

@app.post("/api/termination-groups", dependencies=ADMIN_AUTH)
def create_termination_group(data: TerminationGroupIn):
    if not data.gateway_name.strip() and not db.split_ip_list(data.ips):
        raise HTTPException(400, "Укажите IP группы или FreeSWITCH gateway")
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO termination_groups (name, ips, gateway_name, active) VALUES (?, ?, ?, ?)",
            (data.name, data.ips, data.gateway_name.strip(), int(data.active)),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    except db.sqlite3.IntegrityError:
        raise HTTPException(409, "Группа с таким именем уже существует")
    finally:
        conn.close()


@app.get("/api/termination-groups", dependencies=ADMIN_AUTH)
def list_termination_groups():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM termination_groups ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/termination-groups/{gid}", dependencies=ADMIN_AUTH)
def delete_termination_group(gid: int):
    conn = db.get_conn()
    try:
        used = conn.execute("SELECT 1 FROM terminators WHERE gateway_group_id = ? LIMIT 1", (gid,)).fetchone()
        if used is not None:
            raise HTTPException(409, "Группа используется терминатором")
        cur = conn.execute("DELETE FROM termination_groups WHERE id = ?", (gid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Группа не найдена")
        return {"ok": True}
    finally:
        conn.close()


# ================= CRUD терминаторов (поставщиков) =================

@app.post("/api/terminators", dependencies=ADMIN_AUTH)
def create_terminator(data: TerminatorIn):
    conn = db.get_conn()
    try:
        group = db.get_termination_group(conn, data.gateway_group_id)
        if data.gateway_group_id is not None and group is None:
            raise HTTPException(404, "Терминационная группа не найдена")
        if group is None and not data.gateway_name.strip() and not db.split_ip_list(data.ips):
            raise HTTPException(400, "Укажите gateway или IP терминатора")
        conn.execute("BEGIN IMMEDIATE")
        # если новый терминатор активен — гасим других активных на этот же префикс
        if data.active:
            conn.execute("UPDATE terminators SET active = 0 WHERE prefix = ?", (data.prefix,))
        cur = conn.execute(
            "INSERT INTO terminators (name, gateway_group_id, ips, destination_name, prefix, gateway_name, tech_prefix, cost_rate_cents, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data.name, data.gateway_group_id, data.ips, data.destination_name, data.prefix, data.gateway_name,
             data.tech_prefix, data.cost_rate_cents, int(data.active)),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/api/terminators", dependencies=ADMIN_AUTH)
def list_terminators():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM terminators ORDER BY prefix, active DESC, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/terminators/{tid}", dependencies=ADMIN_AUTH)
def update_terminator(tid: int, data: TerminatorUpdateIn):
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM terminators WHERE id = ?", (tid,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(404, "Терминатор не найден")
        next_group_id = fields.get("gateway_group_id", row["gateway_group_id"] if "gateway_group_id" in row.keys() else None)
        group = db.get_termination_group(conn, next_group_id)
        if next_group_id is not None and group is None:
            conn.rollback()
            raise HTTPException(404, "Терминационная группа не найдена")
        next_gateway = fields.get("gateway_name", row["gateway_name"]) or ""
        next_ips = fields.get("ips", row["ips"]) or ""
        if group is None and not next_gateway.strip() and not db.split_ip_list(next_ips):
            conn.rollback()
            raise HTTPException(400, "Укажите gateway или IP терминатора")
        if fields.get("active"):
            prefix = fields.get("prefix", row["prefix"])
            conn.execute("UPDATE terminators SET active = 0 WHERE prefix = ? AND id != ?", (prefix, tid))
        if "active" in fields:
            fields["active"] = int(fields["active"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE terminators SET {sets} WHERE id = ?", (*fields.values(), tid))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/terminators/{tid}/activate", dependencies=ADMIN_AUTH)
def activate_terminator(tid: int):
    """Сделать терминатор активным (остальные на этот префикс — в резерв)."""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM terminators WHERE id = ?", (tid,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(404, "Терминатор не найден")
        conn.execute("UPDATE terminators SET active = 0 WHERE prefix = ?", (row["prefix"],))
        conn.execute("UPDATE terminators SET active = 1 WHERE id = ?", (tid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/terminators/{tid}", dependencies=ADMIN_AUTH)
def delete_terminator(tid: int):
    conn = db.get_conn()
    try:
        cur = conn.execute("DELETE FROM terminators WHERE id = ?", (tid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Терминатор не найден")
        return {"ok": True}
    finally:
        conn.close()


# ================= CRUD тарифов клиентов =================

@app.post("/api/client-rates", dependencies=ADMIN_AUTH)
def create_client_rate(data: ClientRateIn):
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM clients WHERE id = ?", (data.client_id,)).fetchone()
        if exists is None:
            raise HTTPException(404, "Клиент не найден")
        cur = conn.execute(
            "INSERT INTO client_rates (client_id, terminator_id, prefix, destination_name, sell_rate_cents) "
            "VALUES (?, ?, ?, ?, ?)",
            (data.client_id, data.terminator_id, data.prefix, data.destination_name, data.sell_rate_cents),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/api/client-rates", dependencies=ADMIN_AUTH)
def list_client_rates():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT cr.*, c.name AS client_name, t.name AS terminator_name FROM client_rates cr "
        "JOIN clients c ON c.id = cr.client_id "
        "LEFT JOIN terminators t ON t.id = cr.terminator_id ORDER BY cr.client_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/client-rates/{rid}", dependencies=ADMIN_AUTH)
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


@app.delete("/api/client-rates/{rid}", dependencies=ADMIN_AUTH)
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

@app.get("/api/dashboard-data", dependencies=ADMIN_AUTH)
def dashboard_data():
    conn = db.get_conn()

    clients = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
    groups = conn.execute("SELECT * FROM termination_groups ORDER BY name").fetchall()
    terminators = conn.execute(
        "SELECT t.*, g.name AS gateway_group_name, g.ips AS gateway_group_ips, "
        "g.gateway_name AS gateway_group_gateway_name "
        "FROM terminators t LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
        "ORDER BY t.prefix, t.active DESC, t.id"
    ).fetchall()
    client_rates = conn.execute(
        "SELECT cr.*, c.name AS client_name, t.name AS terminator_name FROM client_rates cr "
        "JOIN clients c ON c.id = cr.client_id "
        "LEFT JOIN terminators t ON t.id = cr.terminator_id ORDER BY cr.client_id"
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
        "termination_groups": [dict(r) for r in groups],
        "terminators": [dict(r) for r in terminators],
        "client_rates": [dict(r) for r in client_rates],
        "cdr": [dict(r) for r in cdr],
        "summary": {
            "total_balance_cents": total_balance,
            "margin_today_cents": margin_today,
            "margin_month_cents": margin_month,
        },
    }


@app.get("/", response_class=HTMLResponse, dependencies=ADMIN_AUTH)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})
