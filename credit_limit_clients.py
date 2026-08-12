from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from credit_limit_common import client_row, ensure_schema


class CreditClientIn(BaseModel):
    name: str
    sip_ip: str
    currency: str = "USD"
    balance_cents: int = Field(default=0, ge=0)
    credit_limit_cents: int = Field(default=0, ge=0)
    active: bool = True


class CreditClientUpdateIn(BaseModel):
    name: Optional[str] = None
    sip_ip: Optional[str] = None
    currency: Optional[str] = None
    active: Optional[bool] = None
    credit_limit_cents: Optional[int] = Field(default=None, ge=0)


def install_client_routes(app, main, db):
    @app.post("/api/clients", dependencies=main.ADMIN_WRITE_AUTH)
    def create_client(data: CreditClientIn):
        ensure_schema(db)
        conn = db.get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO clients (name, sip_ip, balance_cents, credit_limit_cents, currency, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (data.name, data.sip_ip, data.balance_cents, data.credit_limit_cents, data.currency, int(data.active)),
            )
            conn.commit()
            return {"id": cur.lastrowid}
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, f"IP {data.sip_ip} уже используется")
        finally:
            conn.close()

    @app.get("/api/clients", dependencies=main.ADMIN_AUTH)
    def list_clients():
        ensure_schema(db)
        conn = db.get_conn()
        try:
            rows = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
            return [client_row(row) for row in rows]
        finally:
            conn.close()

    @app.patch("/api/clients/{cid}", dependencies=main.ADMIN_WRITE_AUTH)
    def update_client(cid: int, data: CreditClientUpdateIn):
        fields = data.model_dump(exclude_none=True) if hasattr(data, "model_dump") else data.dict(exclude_none=True)
        if not fields:
            raise HTTPException(400, "Нет полей для обновления")
        if "active" in fields:
            fields["active"] = int(fields["active"])
        ensure_schema(db)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
            if client is None:
                conn.rollback()
                raise HTTPException(404, "Клиент не найден")
            if "credit_limit_cents" in fields and int(client["balance_cents"] or 0) < -int(fields["credit_limit_cents"] or 0):
                conn.rollback()
                raise HTTPException(409, "Кредитный лимит ниже текущего минусового баланса")
            sets = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*fields.values(), cid))
            row = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
            conn.commit()
            return {"ok": True, "client": client_row(row)}
        except db.sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(409, "IP уже используется")
        finally:
            conn.close()
