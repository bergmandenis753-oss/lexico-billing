"""
db.py — работа с SQLite. Все деньги в целых центах (int), без float.

Модель разведена на два уровня:
  routes        — КУДА я отправляю трафик: направление + gateway у поставщика +
                  закупочная цена. На одно направление (префикс) может быть
                  несколько роутов, но активен всегда ровно один (ручной выбор).
  client_rates  — по какой цене я ПРОДАЮ каждому клиенту это направление,
                  включая опциональный входящий техпрефикс клиента.

При звонке: клиент по IP → его тариф (sell) по клиентскому техпрефиксу и
префиксу направления → активный роут по префиксу (gateway + cost).
Маржа = sell - cost.

Concurrency: холды (reservations) + BEGIN IMMEDIATE + WAL, чтобы параллельные
звонки одного клиента не потратили баланс дважды.
"""

import hashlib
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).with_name("billing.db")


def _database_path():
    explicit_path = os.getenv("BILLING_DB_PATH") or os.getenv("DB_PATH")
    if explicit_path:
        return Path(explicit_path).expanduser()

    volume_mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_mount:
        return Path(volume_mount).expanduser() / "billing.db"

    return DEFAULT_DB_PATH


DB_PATH = _database_path()

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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        CREATE TABLE IF NOT EXISTS termination_groups (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL UNIQUE, -- account/gateway name: 'Lexico'
            ips              TEXT    NOT NULL DEFAULT '', -- IP через запятую
            gateway_name     TEXT    NOT NULL DEFAULT '', -- опциональный FreeSWITCH gateway
            active           INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS terminators (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL,       -- 'Lexico'
            gateway_group_id INTEGER REFERENCES termination_groups(id),
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
            client_tech_prefix TEXT   NOT NULL DEFAULT '',
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
    # Мягкая миграция: добавляем новые поля в старые таблицы.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(client_rates)").fetchall()]
    if "terminator_id" not in cols:
        conn.execute("ALTER TABLE client_rates ADD COLUMN terminator_id INTEGER")
    if "client_tech_prefix" not in cols:
        conn.execute("ALTER TABLE client_rates ADD COLUMN client_tech_prefix TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crates_client_tech "
        "ON client_rates(client_id, client_tech_prefix, prefix)"
    )
    term_cols = [r["name"] for r in conn.execute("PRAGMA table_info(terminators)").fetchall()]
    if "gateway_group_id" not in term_cols:
        conn.execute("ALTER TABLE terminators ADD COLUMN gateway_group_id INTEGER")
    conn.commit()
    conn.close()


# ---------- выборки ----------

def split_ip_list(value):
    separators_normalized = (value or "").replace("\n", ",").replace(";", ",")
    return [ip.strip() for ip in separators_normalized.split(",") if ip.strip()]


def pick_ip(value, seed=""):
    ips = split_ip_list(value)
    if not ips:
        return ""
    if len(ips) == 1:
        return ips[0]
    digest = hashlib.sha256(str(seed or "").encode("utf-8")).hexdigest()
    return ips[int(digest, 16) % len(ips)]


def get_client_by_ip(conn, sip_ip):
    sip_ip = (sip_ip or "").strip()
    row = conn.execute("SELECT * FROM clients WHERE sip_ip = ?", (sip_ip,)).fetchone()
    if row is not None:
        return row

    rows = conn.execute("SELECT * FROM clients").fetchall()
    for row in rows:
        if sip_ip in split_ip_list(row["sip_ip"]):
            return row
    return None


def match_client_rate(conn, client_id, destination):
    """Тариф клиента по самому длинному подходящему префиксу."""
    match = match_client_rate_for_destination(conn, client_id, destination)
    return match[0] if match is not None else None


def match_client_rate_for_destination(conn, client_id, destination):
    """Тариф клиента + номер без входящего клиентского техпрефикса."""
    destination = str(destination or "")
    rows = conn.execute(
        "SELECT * FROM client_rates WHERE client_id = ? "
        "ORDER BY length(client_tech_prefix) DESC, length(prefix) DESC, id",
        (client_id,),
    ).fetchall()
    for r in rows:
        client_tech_prefix = (r["client_tech_prefix"] or "") if "client_tech_prefix" in r.keys() else ""
        routed_destination = destination
        if client_tech_prefix:
            if not destination.startswith(client_tech_prefix):
                continue
            routed_destination = destination[len(client_tech_prefix):]
            if not routed_destination:
                continue
        if routed_destination.startswith(r["prefix"]):
            return r, routed_destination, client_tech_prefix
    return None


def get_terminator(conn, tid):
    """Терминатор по id (для персонального роута оригинатора)."""
    if tid is None:
        return None
    return conn.execute("SELECT * FROM terminators WHERE id = ?", (tid,)).fetchone()


def get_termination_group(conn, gid):
    """Терминационная группа/account по id."""
    if gid is None:
        return None
    return conn.execute("SELECT * FROM termination_groups WHERE id = ?", (gid,)).fetchone()


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
