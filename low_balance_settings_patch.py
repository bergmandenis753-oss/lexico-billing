import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import HTTPException
from pydantic import BaseModel


SETTING_KEY = "low_balance_threshold_cents"
DEFAULT_THRESHOLD_USD = "10"


class LowBalanceThresholdIn(BaseModel):
    threshold_usd: str | int | float


def ensure_schema(db):
    conn = db.get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ops_settings ("
            "key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL"
            ")"
        )
        conn.commit()
    finally:
        conn.close()


def _parse_threshold_cents(value, scale):
    raw = str(value or "").strip().replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Порог должен быть числом, например 10 или 10.50")
    if amount <= 0:
        raise ValueError("Порог должен быть больше нуля")
    return int((amount * int(scale)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _default_threshold_cents(db):
    raw = os.getenv("LOW_BALANCE_THRESHOLD_USD", DEFAULT_THRESHOLD_USD)
    try:
        return _parse_threshold_cents(raw, db.MONEY_SCALE)
    except ValueError:
        return _parse_threshold_cents(DEFAULT_THRESHOLD_USD, db.MONEY_SCALE)


def get_low_balance_threshold_cents(db):
    ensure_schema(db)
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT value FROM ops_settings WHERE key = ?", (SETTING_KEY,)).fetchone()
        if row:
            try:
                threshold = int(row["value"])
            except (TypeError, ValueError):
                threshold = 0
            if threshold > 0:
                return threshold
        return _default_threshold_cents(db)
    finally:
        conn.close()


def set_low_balance_threshold_cents(db, threshold_cents):
    threshold = int(threshold_cents)
    if threshold <= 0:
        raise ValueError("Порог должен быть больше нуля")
    ensure_schema(db)
    conn = db.get_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO ops_settings (key, value) VALUES (?, ?)", (SETTING_KEY, str(threshold)))
        conn.commit()
        return threshold
    finally:
        conn.close()


def _threshold_units_text(threshold_cents, scale):
    amount = Decimal(int(threshold_cents)) / Decimal(int(scale))
    text = format(amount, "f").rstrip("0").rstrip(".")
    return text or "0"


def _payload(db, threshold_cents=None):
    threshold = int(threshold_cents or get_low_balance_threshold_cents(db))
    return {
        "ok": True,
        "threshold_cents": threshold,
        "threshold_usd": _threshold_units_text(threshold, db.MONEY_SCALE),
        "money_scale": db.MONEY_SCALE,
        "currency": "USD",
    }


def install(app, main, db):
    ensure_schema(db)

    @app.on_event("startup")
    def _low_balance_settings_startup():
        ensure_schema(db)

    @app.get("/api/ops/low-balance-threshold", dependencies=main.API_AUTH)
    def get_low_balance_threshold():
        return _payload(db)

    @app.post("/api/ops/low-balance-threshold", dependencies=main.API_AUTH)
    def set_low_balance_threshold(data: LowBalanceThresholdIn):
        try:
            threshold = _parse_threshold_cents(data.threshold_usd, db.MONEY_SCALE)
            set_low_balance_threshold_cents(db, threshold)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _payload(db, threshold)

    @app.delete("/api/ops/low-balance-threshold", dependencies=main.API_AUTH)
    def reset_low_balance_threshold():
        ensure_schema(db)
        conn = db.get_conn()
        try:
            conn.execute("DELETE FROM ops_settings WHERE key = ?", (SETTING_KEY,))
            conn.commit()
        finally:
            conn.close()
        return _payload(db)
