from pydantic import BaseModel


class ClientTelegramAlertIn(BaseModel):
    chat_id: str
    enabled: bool = True


def _column_names(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_schema(db):
    conn = db.get_conn()
    try:
        columns = _column_names(conn, "clients")
        if "telegram_chat_id" not in columns:
            conn.execute("ALTER TABLE clients ADD COLUMN telegram_chat_id TEXT NOT NULL DEFAULT ''")
        if "telegram_alerts_enabled" not in columns:
            conn.execute("ALTER TABLE clients ADD COLUMN telegram_alerts_enabled INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def _client_payload(client):
    return {
        "id": client["id"],
        "name": client["name"],
        "telegram_chat_id": client["telegram_chat_id"] or "",
        "telegram_alerts_enabled": bool(client["telegram_alerts_enabled"]),
    }


def install(app, main, db):
    ensure_schema(db)

    @app.on_event("startup")
    def _telegram_alerts_startup():
        ensure_schema(db)

    @app.get("/api/ops/client-telegram-alerts", dependencies=main.API_AUTH)
    def list_client_telegram_alerts():
        ensure_schema(db)
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, telegram_chat_id, telegram_alerts_enabled FROM clients ORDER BY name, id"
            ).fetchall()
            return {"ok": True, "clients": [_client_payload(row) for row in rows]}
        finally:
            conn.close()

    @app.post("/api/ops/clients/{client_id}/telegram-alert-chat", dependencies=main.API_AUTH)
    def set_client_telegram_alert(client_id: int, data: ClientTelegramAlertIn):
        chat_id = str(data.chat_id or "").strip()
        if not chat_id:
            raise main.HTTPException(400, "telegram chat_id is required")
        ensure_schema(db)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            client = conn.execute(
                "SELECT id, name, telegram_chat_id, telegram_alerts_enabled FROM clients WHERE id = ?",
                (client_id,),
            ).fetchone()
            if client is None:
                conn.rollback()
                raise main.HTTPException(404, "Клиент не найден")
            conn.execute(
                "UPDATE clients SET telegram_chat_id = ?, telegram_alerts_enabled = ? WHERE id = ?",
                (chat_id, int(bool(data.enabled)), client_id),
            )
            updated = conn.execute(
                "SELECT id, name, telegram_chat_id, telegram_alerts_enabled FROM clients WHERE id = ?",
                (client_id,),
            ).fetchone()
            conn.commit()
            return {"ok": True, "client": _client_payload(updated)}
        finally:
            conn.close()

    @app.delete("/api/ops/clients/{client_id}/telegram-alert-chat", dependencies=main.API_AUTH)
    def clear_client_telegram_alert(client_id: int):
        ensure_schema(db)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            client = conn.execute(
                "SELECT id, name, telegram_chat_id, telegram_alerts_enabled FROM clients WHERE id = ?",
                (client_id,),
            ).fetchone()
            if client is None:
                conn.rollback()
                raise main.HTTPException(404, "Клиент не найден")
            conn.execute(
                "UPDATE clients SET telegram_chat_id = '', telegram_alerts_enabled = 0 WHERE id = ?",
                (client_id,),
            )
            conn.commit()
            return {"ok": True, "client_id": client_id}
        finally:
            conn.close()
