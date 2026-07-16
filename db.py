"""
db.py — работа с SQLite. Все деньги хранятся в целых центах (int), без float.

Ключевая идея по concurrency: у одного клиента может идти несколько
параллельных звонков. Чтобы два одновременных reserve не потратили один и тот же
баланс дважды, при reserve мы создаём "холд" (строку в reservations) на всю
доступную сумму под этот звонок. Доступный баланс = balance_cents минус сумма
активных (непросроченных) холдов. Всё это делается в одной транзакции
BEGIN IMMEDIATE, поэтому параллельные запросы сериализуются на уровне БД.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("billing.db")

# Запас поверх max_seconds, чтобы холд не протух пока звонок ещё идёт (сек).
RESERVATION_BUFFER_SEC = 120

# --- Правила тарификации ---
# Тарифы в rates заданы за МИНУТУ. Ниже — как округляем длительность звонка.
# Пример настроек: посекундно = (1, 1); минимум 30с потом по 6с = (30, 6);
# поминутно = (60, 60).
MIN_BILL_SEC = 1        # минимальная тарифицируемая длительность (сек)
BILL_INCREMENT_SEC = 1  # шаг округления после минимума (сек)


def billed_seconds(billsec: int) -> int:
    """Округляем фактические секунды по правилам тарификации."""
    if billsec <= 0:
        return 0
    if billsec <= MIN_BILL_SEC:
        return MIN_BILL_SEC
    # округляем вверх до кратного шагу
    extra = billsec - MIN_BILL_SEC
    steps = -(-extra // BILL_INCREMENT_SEC)  # ceil-деление
    return MIN_BILL_SEC + steps * BILL_INCREMENT_SEC


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL — чтобы чтение дашборда не блокировало запись биллинга.
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
            sip_ip        TEXT    NOT NULL UNIQUE,   -- идентификация клиента по IP (whitelist)
            balance_cents INTEGER NOT NULL DEFAULT 0,
            currency      TEXT    NOT NULL DEFAULT 'USD',
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rates (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            prefix           TEXT    NOT NULL,   -- напр. '61'
            destination_name TEXT    NOT NULL,   -- напр. 'Australia'
            cost_rate_cents  INTEGER NOT NULL,   -- закупка у Lexico за минуту
            sell_rate_cents  INTEGER NOT NULL    -- продажа клиенту за минуту
        );
        -- Быстрый подбор тарифа по клиенту и префиксу.
        CREATE INDEX IF NOT EXISTS idx_rates_client ON rates(client_id, prefix);

        CREATE TABLE IF NOT EXISTS cdr (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id       INTEGER NOT NULL REFERENCES clients(id),
            call_uuid       TEXT,
            destination     TEXT,
            billsec         INTEGER NOT NULL,
            sell_rate_cents INTEGER NOT NULL,
            cost_rate_cents INTEGER NOT NULL,
            charged_cents   INTEGER NOT NULL,
            margin_cents    INTEGER NOT NULL,
            started_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cdr_started ON cdr(started_at);

        -- Активные холды под идущие звонки.
        CREATE TABLE IF NOT EXISTS reservations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id      INTEGER NOT NULL REFERENCES clients(id),
            call_uuid      TEXT    NOT NULL,
            reserved_cents INTEGER NOT NULL,
            expires_at     INTEGER NOT NULL,   -- unix-время, после которого холд считается протухшим
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_resv_client ON reservations(client_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_resv_uuid ON reservations(call_uuid);
        """
    )
    conn.commit()
    conn.close()


# ---------- вспомогательные выборки ----------

def get_client_by_ip(conn, sip_ip):
    return conn.execute("SELECT * FROM clients WHERE sip_ip = ?", (sip_ip,)).fetchone()


def match_rate(conn, client_id, destination):
    """
    Подбираем тариф клиента по самому длинному префиксу, который совпадает
    с началом набранного номера. Так '61' поймает австралийские номера.
    """
    rows = conn.execute(
        "SELECT * FROM rates WHERE client_id = ? ORDER BY length(prefix) DESC",
        (client_id,),
    ).fetchall()
    for r in rows:
        if destination.startswith(r["prefix"]):
            return r
    return None


def active_hold_sum(conn, client_id, now_ts):
    """Сумма непротухших холдов клиента."""
    row = conn.execute(
        "SELECT COALESCE(SUM(reserved_cents), 0) AS s FROM reservations "
        "WHERE client_id = ? AND expires_at > ?",
        (client_id, now_ts),
    ).fetchone()
    return row["s"]


def cleanup_expired(conn, now_ts):
    """Удаляем протухшие холды (звонки, которые не дошли до finalize)."""
    conn.execute("DELETE FROM reservations WHERE expires_at <= ?", (now_ts,))


def now() -> int:
    return int(time.time())
