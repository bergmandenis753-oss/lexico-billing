"""
db.py — работа с SQLite. Все деньги в целых центах (int), без float.

Модель разведена на два уровня:
  routes        — КУДА я отправляю трафик: направление + gateway у поставщика +
                  закупочная цена. На одно направление (префикс) может быть
                  несколько роутов, но активен всегда ровно один (ручной выбор).
  client_rates  — по какой цене я ПРОДАЮ каждому клиенту это направление.

При звонке: клиент по IP → его тариф (sell) по префиксу → активный роут по
префиксу (gateway + cost). Маржа = sell - cost.

Concurrency: холды (reservations) + BEGIN IMMEDIATE + WAL, чтобы параллельные
звонки одного клиента не потратили баланс дважды.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("billing.db")

RESERVATION_BUFFER_SEC = 120

# Правила тарификации (тариф задан за минуту). Посекундно = (1, 1).
MIN_BILL_SEC = 1
BILL_INCREMENT_SEC = 1


def billed_seconds(billsec: int) -> int:
    if billsec <= 0:
        return 0
    if billsec <= MIN_BILL_SEC:
        return MIN_BILL_SEC
    extra = billsec - MIN_BILL_SEC
    steps = -(-extra // BILL_INCREMENT_SEC)  # ceil-деление
    return MIN_BILL_SEC + steps * BILL_INCREMENT_SEC


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            sip_ip        TEXT    NOT NULL UNIQUE,   -- whitelist по IP
            balance_cents INTEGER NOT NULL DEFAULT 0,
            currency      TEXT    NOT NULL DEFAULT 'USD',
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Терминаторы (поставщики, кто даёт нам роут — напр. Lexico). Закупка.
        -- На префикс активен ровно один терминатор.
        CREATE TABLE IF NOT EXISTS terminators (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL,       -- 'Lexico'
            ips              TEXT    NOT NULL DEFAULT '',  -- IP поставщика через запятую: '195.219.39.9,195.219.39.14'
            destination_name TEXT    NOT NULL,       -- 'Australia'
            prefix           TEXT    NOT NULL,       -- '61'
            gateway_name     TEXT    NOT NULL,       -- имя sofia-gateway, напр. 'lexico'
            tech_prefix      TEXT    NOT NULL DEFAULT '',  -- техпрефикс перед номером, напр. '999001'
            cost_rate_cents  INTEGER NOT NULL,       -- закупка за минуту
            active           INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_term_prefix ON terminators(prefix, active);

        -- Роуты оригинаторов: направление + цена продажи + ПЕРСОНАЛЬНЫЙ терминатор.
        -- terminator_id — через какой терминатор идёт трафик именно этого оригинатора.
        CREATE TABLE IF NOT EXISTS client_rates (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            terminator_id    INTEGER REFERENCES terminators(id),
            prefix           TEXT    NOT NULL,
            destination_name TEXT    NOT NULL,
            sell_rate_cents  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_crates_client ON client_rates(client_id, prefix);

        CREATE TABLE IF NOT EXISTS cdr (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id       INTEGER NOT NULL REFERENCES clients(id),
            call_uuid       TEXT,
            destination     TEXT,
            gateway_name    TEXT,
            billsec         INTEGER NOT NULL,
            sell_rate_cents INTEGER NOT NULL,
            cost_rate_cents INTEGER NOT NULL,
            charged_cents   INTEGER NOT NULL,
            margin_cents    INTEGER NOT NULL,
            started_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cdr_started ON cdr(started_at);

        CREATE TABLE IF NOT EXISTS reservations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id      INTEGER NOT NULL REFERENCES clients(id),
            call_uuid      TEXT    NOT NULL,
            reserved_cents INTEGER NOT NULL,
            expires_at     INTEGER NOT NULL,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_resv_client ON reservations(client_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_resv_uuid ON reservations(call_uuid);
        """
    )
    # Мягкая миграция: добавляем terminator_id в старую таблицу client_rates.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(client_rates)").fetchall()]
    if "terminator_id" not in cols:
        conn.execute("ALTER TABLE client_rates ADD COLUMN terminator_id INTEGER")
    conn.commit()
    conn.close()


# ---------- выборки ----------

def get_client_by_ip(conn, sip_ip):
    return conn.execute("SELECT * FROM clients WHERE sip_ip = ?", (sip_ip,)).fetchone()


def match_client_rate(conn, client_id, destination):
    """Тариф клиента по самому длинному подходящему префиксу."""
    rows = conn.execute(
        "SELECT * FROM client_rates WHERE client_id = ? ORDER BY length(prefix) DESC",
        (client_id,),
    ).fetchall()
    for r in rows:
        if destination.startswith(r["prefix"]):
            return r
    return None


def get_terminator(conn, tid):
    """Терминатор по id (для персонального роута оригинатора)."""
    if tid is None:
        return None
    return conn.execute("SELECT * FROM terminators WHERE id = ?", (tid,)).fetchone()


def match_active_terminator(conn, destination):
    """Активный терминатор по самому длинному подходящему префиксу."""
    rows = conn.execute(
        "SELECT * FROM terminators WHERE active = 1 ORDER BY length(prefix) DESC",
    ).fetchall()
    for r in rows:
        if destination.startswith(r["prefix"]):
            return r
    return None


def active_hold_sum(conn, client_id, now_ts):
    row = conn.execute(
        "SELECT COALESCE(SUM(reserved_cents), 0) AS s FROM reservations "
        "WHERE client_id = ? AND expires_at > ?",
        (client_id, now_ts),
    ).fetchone()
    return row["s"]


def cleanup_expired(conn, now_ts):
    conn.execute("DELETE FROM reservations WHERE expires_at <= ?", (now_ts,))


def now() -> int:
    return int(time.time())
