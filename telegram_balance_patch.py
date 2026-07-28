from fastapi import HTTPException
from pydantic import BaseModel, Field


class BotTopupIn(BaseModel):
    amount_cents: int = Field(gt=0)
    note: str = "telegram bot"


def install(app, main, db):
    @app.post("/api/ops/clients/{client_id}/topup", dependencies=main.API_AUTH)
    def telegram_bot_topup(client_id: int, data: BotTopupIn):
        amount = int(data.amount_cents)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            client = conn.execute(
                "SELECT id, name, currency, balance_cents FROM clients WHERE id=?",
                (client_id,),
            ).fetchone()
            if not client:
                conn.rollback()
                raise HTTPException(status_code=404, detail="Client not found")

            old_balance = int(client["balance_cents"] or 0)
            new_balance = old_balance + amount
            conn.execute(
                "UPDATE clients SET balance_cents=? WHERE id=?",
                (new_balance, client_id),
            )
            conn.commit()
            return {
                "ok": True,
                "client_id": client_id,
                "client_name": client["name"],
                "currency": client["currency"] or "USD",
                "amount_cents": amount,
                "old_balance_cents": old_balance,
                "balance_cents": new_balance,
                "money_scale": db.MONEY_SCALE,
            }
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
