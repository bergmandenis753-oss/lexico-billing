"""
main.py — FastAPI приложение биллинга.

Запуск для разработки:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Точки для FreeSWITCH:
    POST /api/reserve   — перед соединением звонка
    POST /api/finalize  — после окончания звонка
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


# ---------------- Pydantic-схемы входа ----------------

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


class RateIn(BaseModel):
    client_id: int
    prefix: str
    destination_name: str
    cost_rate_cents: int = Field(ge=0)
    sell_rate_cents: int = Field(ge=0)


class RateUpdateIn(BaseModel):
    prefix: Optional[str] = None
    destination_name: Optional[str] = None
    cost_rate_cents: Optional[int] = None
    sell_rate_cents: Optional[int] = None


# ================= БИЛЛИНГ =================

@app.post("/api/reserve")
def reserve(data: ReserveIn):
    """
    Вызывается FreeSWITCH ПЕРЕД соединением. Создаёт холд на доступный баланс,
    чтобы параллельные звонки того же клиента не потратили деньги дважды.
    """
    conn = db.get_conn()
    try:
        now_ts = db.now()
        # BEGIN IMMEDIATE берёт write-lock сразу — параллельные reserve сериализуются.
        conn.execute("BEGIN IMMEDIATE")
        db.cleanup_expired(conn, now_ts)

        client = db.get_client_by_ip(conn, data.sip_ip)
        if client is None:
            raise HTTPException(403, f"Клиент с IP {data.sip_ip} не найден")
        if not client["active"]:
            raise HTTPException(403, "Клиент неактивен")

        rate = db.match_rate(conn, client["id"], data.destination)
        if rate is None:
            raise HTTPException(403, f"Нет тарифа для номера {data.destination}")
        if rate["sell_rate_cents"] <= 0:
            raise HTTPException(403, "Некорректный тариф продажи (<= 0)")

        held = db.active_hold_sum(conn, client["id"], now_ts)
        available = client["balance_cents"] - held
        if available <= 0:
            raise HTTPException(403, "Недостаточно средств (баланс занят активными звонками)")

        # Сколько секунд клиент может проговорить на доступные деньги.
        max_seconds = math.floor(available / rate["sell_rate_cents"] * 60)
        if max_seconds <= 0:
            raise HTTPException(403, "Недостаточно средств на минуту разговора")

        # Холд на всю доступную сумму под этот звонок; протухает после max_seconds + буфер.
        call_uuid = data.call_uuid or f"noуuid-{now_ts}-{client['id']}"
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
            "cost_rate_cents": rate["cost_rate_cents"],
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
    """
    Вызывается ПОСЛЕ окончания звонка. Списывает фактическую стоимость,
    снимает холд и пишет CDR. Тарификация поминутная (ceil).
    """
    conn = db.get_conn()
    try:
        # Посекундная (настраиваемая) тарификация: тариф задан за минуту,
        # берём округлённые секунды и считаем долю минуты. ceil — чтобы не недосписать.
        bsec = db.billed_seconds(data.billsec)
        charged = math.ceil(bsec * data.sell_rate_cents / 60)
        cost = math.ceil(bsec * data.cost_rate_cents / 60)  # наша себестоимость у Lexico

        conn.execute("BEGIN IMMEDIATE")
        client = conn.execute(
            "SELECT * FROM clients WHERE id = ?", (data.client_id,)
        ).fetchone()
        if client is None:
            raise HTTPException(404, "Клиент не найден")

        # Не уходим в минус: если списать нечем — clamp на 0 и логируем расхождение.
        new_balance = client["balance_cents"] - charged
        if new_balance < 0:
            print(
                f"[FINALIZE WARN] client={data.client_id} call={data.call_uuid} "
                f"charged={charged} > balance={client['balance_cents']}: clamp to 0"
            )
            charged = client["balance_cents"]
            new_balance = 0

        # Маржа = сколько реально списали минус себестоимость. При clamp'е
        # (клиент ушёл бы в минус) маржа честно покажет убыток.
        margin = charged - cost

        conn.execute(
            "UPDATE clients SET balance_cents = ? WHERE id = ?",
            (new_balance, data.client_id),
        )
        conn.execute(
            "INSERT INTO cdr (client_id, call_uuid, destination, billsec, "
            "sell_rate_cents, cost_rate_cents, charged_cents, margin_cents) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.client_id, data.call_uuid, data.destination, data.billsec,
                data.sell_rate_cents, data.cost_rate_cents, charged, margin,
            ),
        )
        # Снимаем холд этого звонка.
        conn.execute("DELETE FROM reservations WHERE call_uuid = ?", (data.call_uuid,))
        conn.commit()

        return {
            "ok": True,
            "charged_cents": charged,
            "margin_cents": margin,
            "balance_cents": new_balance,
        }
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
            "INSERT INTO clients (name, sip_ip, balance_cents, currency, active) "
            "VALUES (?, ?, ?, ?, ?)",
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


@app.get("/api/clients/{cid}")
def get_client(cid: int):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, "Клиент не найден")
    return dict(row)


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
        cur = conn.execute(
            f"UPDATE clients SET {sets} WHERE id = ?", (*fields.values(), cid)
        )
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
    """Ручное пополнение баланса (клиент оплатил вне системы)."""
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


# ================= CRUD тарифов =================

@app.post("/api/rates")
def create_rate(data: RateIn):
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM clients WHERE id = ?", (data.client_id,)).fetchone()
        if exists is None:
            raise HTTPException(404, "Клиент не найден")
        cur = conn.execute(
            "INSERT INTO rates (client_id, prefix, destination_name, cost_rate_cents, sell_rate_cents) "
            "VALUES (?, ?, ?, ?, ?)",
            (data.client_id, data.prefix, data.destination_name,
             data.cost_rate_cents, data.sell_rate_cents),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/api/rates")
def list_rates():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT r.*, c.name AS client_name FROM rates r "
        "JOIN clients c ON c.id = r.client_id ORDER BY r.client_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/rates/{rid}")
def update_rate(rid: int, data: RateUpdateIn):
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = db.get_conn()
    try:
        cur = conn.execute(
            f"UPDATE rates SET {sets} WHERE id = ?", (*fields.values(), rid)
        )
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

    clients = conn.execute(
        """
        SELECT c.*,
               (SELECT r.destination_name FROM rates r WHERE r.client_id = c.id LIMIT 1) AS destination_name,
               (SELECT r.sell_rate_cents  FROM rates r WHERE r.client_id = c.id LIMIT 1) AS sell_rate_cents
        FROM clients c ORDER BY c.id
        """
    ).fetchall()

    rates = conn.execute(
        "SELECT r.*, c.name AS client_name FROM rates r "
        "JOIN clients c ON c.id = r.client_id ORDER BY r.client_id"
    ).fetchall()

    cdr = conn.execute("SELECT * FROM cdr ORDER BY id DESC LIMIT 50").fetchall()

    total_balance = conn.execute(
        "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM clients"
    ).fetchone()["s"]

    margin_today = conn.execute(
        "SELECT COALESCE(SUM(margin_cents), 0) AS s FROM cdr "
        "WHERE date(started_at) = date('now')"
    ).fetchone()["s"]

    margin_month = conn.execute(
        "SELECT COALESCE(SUM(margin_cents), 0) AS s FROM cdr "
        "WHERE strftime('%Y-%m', started_at) = strftime('%Y-%m', 'now')"
    ).fetchone()["s"]

    conn.close()
    return {
        "clients": [dict(r) for r in clients],
        "rates": [dict(r) for r in rates],
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
