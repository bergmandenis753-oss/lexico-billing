"""
db.py — работа с SQLite. Деньги и тарифы хранятся целыми числами без float.

Внутренняя единица = 0.0001 USD. Старые имена колонок *_cents сохранены
для мягкой миграции, но после миграции значения в них уже не центы.

Модель разведена на два уровня:
  terminators   — КУДА я отправляю трафик: направление + gateway у поставщика +
                  закупочная цена.
  client_rates  — по какой цене я ПРОДАЮ каждому клиенту это направление,
                  включая опциональный входящий техпрефикс клиента и конкретный
                  терминатор. Несколько строк с одним client/TP/prefix образуют
                  пул и распределяются равными долями.

При звонке: клиент по IP → его тариф (sell) по клиентскому техпрефиксу и
префиксу направления → терминатор из подходящего пула (gateway + cost).
Маржа = sell - cost.

Concurrency: холды (reservations) + BEGIN IMMEDIATE + WAL, чтобы параллельные
звонки одного клиента не потратили баланс дважды.
"""

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).with_name("billing.db")
E164_DATA_PATH = Path(__file__).with_name("data") / "e164_prefixes.json"


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

# Правила тарификации (тариф задан за минуту). По умолчанию посекундно = 1/1.
DEFAULT_BILLING_CYCLE = "1/1"
BILLING_CYCLE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
MONEY_SCALE = 10000
LEGACY_CENT_TO_MONEY_UNITS = 100


def normalize_billing_cycle(value) -> str:
    match = BILLING_CYCLE_RE.match(str(value or "").strip())
    if not match:
        return DEFAULT_BILLING_CYCLE
    first, increment = int(match.group(1)), int(match.group(2))
    if first <= 0 or increment <= 0:
        return DEFAULT_BILLING_CYCLE
    return f"{first}/{increment}"


def billing_cycle_parts(value):
    first, increment = normalize_billing_cycle(value).split("/", 1)
    return int(first), int(increment)


def default_billing_cycle_for_route(destination_name="", prefix="") -> str:
    digits = normalize_phone_number(prefix)
    direction = canonical_direction_name(destination_name).lower()
    if digits == "86" or direction in {"china", "китай"}:
        return "60/1"
    return DEFAULT_BILLING_CYCLE


def billed_seconds(billsec: int, billing_cycle: str = DEFAULT_BILLING_CYCLE) -> int:
    billsec = int(billsec or 0)
    if billsec <= 0:
        return 0
    first, increment = billing_cycle_parts(billing_cycle)
    if billsec <= first:
        return first
    extra = billsec - first
    steps = -(-extra // increment)  # ceil-деление
    return first + steps * increment


def charge_units(rate_units, billsec, billing_cycle: str = DEFAULT_BILLING_CYCLE) -> int:
    rate_units = int(rate_units or 0)
    if rate_units <= 0:
        return 0
    return (billed_seconds(billsec, billing_cycle) * rate_units + 59) // 60


def max_seconds_for_balance(balance_units, rate_units, billing_cycle: str = DEFAULT_BILLING_CYCLE) -> int:
    balance_units = int(balance_units or 0)
    rate_units = int(rate_units or 0)
    if balance_units <= 0 or rate_units <= 0:
        return 0
    max_billable = (balance_units * 60) // rate_units
    first, increment = billing_cycle_parts(billing_cycle)
    if max_billable < first:
        return 0
    if max_billable <= first or increment <= 1:
        return max_billable
    return first + ((max_billable - first) // increment) * increment


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
            cost_rate_cents  INTEGER NOT NULL,       -- legacy name; 0.0001 USD/min units
            balance_cents    INTEGER NOT NULL DEFAULT 0, -- баланс у поставщика; 0.0001 USD units
            billing_cycle    TEXT    NOT NULL DEFAULT '1/1',
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
            sell_rate_cents  INTEGER NOT NULL,       -- legacy name; 0.0001 USD/min units
            billing_cycle    TEXT    NOT NULL DEFAULT '1/1'
        );
        CREATE INDEX IF NOT EXISTS idx_crates_client ON client_rates(client_id, prefix);

        CREATE TABLE IF NOT EXISTS cdr (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id       INTEGER NOT NULL REFERENCES clients(id),
            call_uuid       TEXT,
            sip_ip          TEXT    NOT NULL DEFAULT '',
            clid            TEXT    NOT NULL DEFAULT '',
            destination     TEXT,
            client_tech_prefix TEXT NOT NULL DEFAULT '',
            dial_destination TEXT   NOT NULL DEFAULT '',
            provider_number TEXT    NOT NULL DEFAULT '',
            gateway_name    TEXT,
            route_ip        TEXT    NOT NULL DEFAULT '',
            terminator_id   INTEGER,
            terminator_name TEXT    NOT NULL DEFAULT '',
            terminator_destination_name TEXT NOT NULL DEFAULT '',
            terminator_prefix TEXT  NOT NULL DEFAULT '',
            terminator_tech_prefix TEXT NOT NULL DEFAULT '',
            hangup_cause    TEXT    NOT NULL DEFAULT '',
            bridge_hangup_cause TEXT NOT NULL DEFAULT '',
            result          TEXT    NOT NULL DEFAULT '',
            billsec         INTEGER NOT NULL,
            sell_rate_cents INTEGER NOT NULL,
            cost_rate_cents INTEGER NOT NULL,
            sell_billing_cycle TEXT NOT NULL DEFAULT '1/1',
            cost_billing_cycle TEXT NOT NULL DEFAULT '1/1',
            charged_cents   INTEGER NOT NULL,
            margin_cents    INTEGER NOT NULL,
            started_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cdr_started ON cdr(started_at);

        CREATE TABLE IF NOT EXISTS sip_hits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            call_uuid       TEXT    NOT NULL DEFAULT '',
            sip_ip          TEXT    NOT NULL DEFAULT '',
            sip_port        TEXT    NOT NULL DEFAULT '',
            clid            TEXT    NOT NULL DEFAULT '',
            destination     TEXT    NOT NULL DEFAULT '',
            client_id       INTEGER,
            client_name     TEXT    NOT NULL DEFAULT '',
            client_tech_prefix TEXT NOT NULL DEFAULT '',
            dial_destination TEXT   NOT NULL DEFAULT '',
            provider_number TEXT    NOT NULL DEFAULT '',
            gateway_name    TEXT    NOT NULL DEFAULT '',
            route_ip        TEXT    NOT NULL DEFAULT '',
            terminator_id   INTEGER,
            terminator_name TEXT    NOT NULL DEFAULT '',
            terminator_destination_name TEXT NOT NULL DEFAULT '',
            terminator_prefix TEXT  NOT NULL DEFAULT '',
            status          TEXT    NOT NULL DEFAULT '',
            stage           TEXT    NOT NULL DEFAULT '',
            reason          TEXT    NOT NULL DEFAULT '',
            max_seconds     INTEGER,
            sell_rate_cents INTEGER NOT NULL DEFAULT 0,
            cost_rate_cents INTEGER NOT NULL DEFAULT 0,
            sell_billing_cycle TEXT NOT NULL DEFAULT '1/1',
            cost_billing_cycle TEXT NOT NULL DEFAULT '1/1',
            user_agent      TEXT    NOT NULL DEFAULT '',
            sip_call_id     TEXT    NOT NULL DEFAULT '',
            profile         TEXT    NOT NULL DEFAULT '',
            context         TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sip_hits_created ON sip_hits(created_at);
        CREATE INDEX IF NOT EXISTS idx_sip_hits_ip ON sip_hits(sip_ip);

        CREATE TABLE IF NOT EXISTS pcap_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            direction       TEXT    NOT NULL DEFAULT '',
            src_ip          TEXT    NOT NULL DEFAULT '',
            src_port        TEXT    NOT NULL DEFAULT '',
            dst_ip          TEXT    NOT NULL DEFAULT '',
            dst_port        TEXT    NOT NULL DEFAULT '',
            method          TEXT    NOT NULL DEFAULT '',
            status_code     INTEGER,
            status_text     TEXT    NOT NULL DEFAULT '',
            call_id         TEXT    NOT NULL DEFAULT '',
            cseq            TEXT    NOT NULL DEFAULT '',
            from_user       TEXT    NOT NULL DEFAULT '',
            to_user         TEXT    NOT NULL DEFAULT '',
            request_uri     TEXT    NOT NULL DEFAULT '',
            user_agent      TEXT    NOT NULL DEFAULT '',
            reason          TEXT    NOT NULL DEFAULT '',
            raw_summary     TEXT    NOT NULL DEFAULT '',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_pcap_events_created ON pcap_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_pcap_events_call_id ON pcap_events(call_id);
        CREATE INDEX IF NOT EXISTS idx_pcap_events_src_ip ON pcap_events(src_ip);

        CREATE TABLE IF NOT EXISTS e164_prefixes (
            prefix          TEXT PRIMARY KEY,
            country         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_e164_prefixes_country ON e164_prefixes(country);

        CREATE TABLE IF NOT EXISTS e164_countries (
            country         TEXT PRIMARY KEY,
            primary_prefix  TEXT NOT NULL DEFAULT '',
            prefix_count    INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL
        );

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
    if "client_tech_prefix" not in cols:
        conn.execute("ALTER TABLE client_rates ADD COLUMN client_tech_prefix TEXT NOT NULL DEFAULT ''")
    if "billing_cycle" not in cols:
        conn.execute("ALTER TABLE client_rates ADD COLUMN billing_cycle TEXT NOT NULL DEFAULT '1/1'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crates_client_tech "
        "ON client_rates(client_id, client_tech_prefix, prefix)"
    )
    term_cols = [r["name"] for r in conn.execute("PRAGMA table_info(terminators)").fetchall()]
    if "gateway_group_id" not in term_cols:
        conn.execute("ALTER TABLE terminators ADD COLUMN gateway_group_id INTEGER")
    if "billing_cycle" not in term_cols:
        conn.execute("ALTER TABLE terminators ADD COLUMN billing_cycle TEXT NOT NULL DEFAULT '1/1'")
    if "balance_cents" not in term_cols:
        conn.execute("ALTER TABLE terminators ADD COLUMN balance_cents INTEGER NOT NULL DEFAULT 0")
    cdr_cols = [r["name"] for r in conn.execute("PRAGMA table_info(cdr)").fetchall()]
    cdr_migrations = {
        "sip_ip": "ALTER TABLE cdr ADD COLUMN sip_ip TEXT NOT NULL DEFAULT ''",
        "clid": "ALTER TABLE cdr ADD COLUMN clid TEXT NOT NULL DEFAULT ''",
        "client_tech_prefix": "ALTER TABLE cdr ADD COLUMN client_tech_prefix TEXT NOT NULL DEFAULT ''",
        "dial_destination": "ALTER TABLE cdr ADD COLUMN dial_destination TEXT NOT NULL DEFAULT ''",
        "provider_number": "ALTER TABLE cdr ADD COLUMN provider_number TEXT NOT NULL DEFAULT ''",
        "route_ip": "ALTER TABLE cdr ADD COLUMN route_ip TEXT NOT NULL DEFAULT ''",
        "terminator_id": "ALTER TABLE cdr ADD COLUMN terminator_id INTEGER",
        "terminator_name": "ALTER TABLE cdr ADD COLUMN terminator_name TEXT NOT NULL DEFAULT ''",
        "terminator_destination_name": "ALTER TABLE cdr ADD COLUMN terminator_destination_name TEXT NOT NULL DEFAULT ''",
        "terminator_prefix": "ALTER TABLE cdr ADD COLUMN terminator_prefix TEXT NOT NULL DEFAULT ''",
        "terminator_tech_prefix": "ALTER TABLE cdr ADD COLUMN terminator_tech_prefix TEXT NOT NULL DEFAULT ''",
        "hangup_cause": "ALTER TABLE cdr ADD COLUMN hangup_cause TEXT NOT NULL DEFAULT ''",
        "bridge_hangup_cause": "ALTER TABLE cdr ADD COLUMN bridge_hangup_cause TEXT NOT NULL DEFAULT ''",
        "result": "ALTER TABLE cdr ADD COLUMN result TEXT NOT NULL DEFAULT ''",
        "sell_billing_cycle": "ALTER TABLE cdr ADD COLUMN sell_billing_cycle TEXT NOT NULL DEFAULT '1/1'",
        "cost_billing_cycle": "ALTER TABLE cdr ADD COLUMN cost_billing_cycle TEXT NOT NULL DEFAULT '1/1'",
    }
    for col, sql in cdr_migrations.items():
        if col not in cdr_cols:
            conn.execute(sql)
    sip_cols = [r["name"] for r in conn.execute("PRAGMA table_info(sip_hits)").fetchall()]
    sip_migrations = {
        "sell_billing_cycle": "ALTER TABLE sip_hits ADD COLUMN sell_billing_cycle TEXT NOT NULL DEFAULT '1/1'",
        "cost_billing_cycle": "ALTER TABLE sip_hits ADD COLUMN cost_billing_cycle TEXT NOT NULL DEFAULT '1/1'",
    }
    for col, sql in sip_migrations.items():
        if col not in sip_cols:
            conn.execute(sql)
    scale_row = conn.execute("SELECT value FROM app_meta WHERE key = 'money_scale'").fetchone()
    if scale_row is None:
        for table, col in (
            ("clients", "balance_cents"),
            ("reservations", "reserved_cents"),
            ("terminators", "cost_rate_cents"),
            ("client_rates", "sell_rate_cents"),
            ("cdr", "sell_rate_cents"),
            ("cdr", "cost_rate_cents"),
            ("cdr", "charged_cents"),
            ("cdr", "margin_cents"),
            ("sip_hits", "sell_rate_cents"),
            ("sip_hits", "cost_rate_cents"),
        ):
            conn.execute(f"UPDATE {table} SET {col} = {col} * ?", (LEGACY_CENT_TO_MONEY_UNITS,))
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('money_scale', ?)",
            (str(MONEY_SCALE),),
        )
    ensure_e164_loaded(conn)
    conn.execute(
        "UPDATE terminators SET billing_cycle = '60/1' "
        "WHERE (prefix = '86' OR lower(destination_name) = 'china' OR destination_name = 'Китай') "
        "AND COALESCE(NULLIF(billing_cycle, ''), '1/1') = '1/1'"
    )
    conn.execute(
        "UPDATE client_rates SET billing_cycle = '60/1' "
        "WHERE (prefix = '86' OR lower(destination_name) = 'china' OR destination_name = 'Китай') "
        "AND COALESCE(NULLIF(billing_cycle, ''), '1/1') = '1/1'"
    )
    conn.commit()
    conn.close()


# ---------- выборки ----------

def split_ip_list(value):
    separators_normalized = (value or "").replace("\n", ",").replace(";", ",")
    return [ip.strip() for ip in separators_normalized.split(",") if ip.strip()]


def ip_token_matches(candidate, token):
    candidate = (candidate or "").strip()
    token = (token or "").strip()
    if not candidate or not token:
        return False
    if candidate == token:
        return True
    if "/" not in token:
        return False
    try:
        return ipaddress.ip_address(candidate) in ipaddress.ip_network(token, strict=False)
    except ValueError:
        return False


def pick_ip(value, seed=""):
    ips = split_ip_list(value)
    if not ips:
        return ""
    if len(ips) == 1:
        return ips[0]
    digest = hashlib.sha256(str(seed or "").encode("utf-8")).hexdigest()
    return ips[int(digest, 16) % len(ips)]


def canonical_direction_name(value):
    value = (value or "").strip()
    value = re.sub(r",\s*Mobile\b", "", value, flags=re.IGNORECASE).strip()
    if not value:
        return ""
    known = {
        "uk": "UK",
        "usa": "USA",
        "u.s.a.": "USA",
    }
    return known.get(value.lower(), value.title() if value.islower() else value)


def normalize_phone_number(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("00") and len(digits) > 2:
        return digits[2:]
    return digits


def _common_prefix(values):
    values = [v for v in values if v]
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while prefix and not value.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def _e164_data_version():
    if not E164_DATA_PATH.exists():
        return ""
    st = E164_DATA_PATH.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def ensure_e164_loaded(conn):
    """Load compact E.164 country prefixes into SQLite once per data version."""
    version = _e164_data_version()
    if not version:
        return
    current = conn.execute("SELECT value FROM app_meta WHERE key = 'e164_data_version'").fetchone()
    count = conn.execute("SELECT COUNT(*) AS c FROM e164_prefixes").fetchone()["c"]
    if current is not None and current["value"] == version and count > 0:
        return

    with E164_DATA_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    prefix_rows = []
    country_rows = []
    for raw_country, prefixes in data.items():
        country = canonical_direction_name(raw_country)
        clean_prefixes = sorted(
            {"".join(ch for ch in str(prefix) if ch.isdigit()) for prefix in prefixes},
            key=lambda p: (len(p), p),
        )
        clean_prefixes = [p for p in clean_prefixes if p]
        if not country or not clean_prefixes:
            continue
        prefix_rows.extend((prefix, country) for prefix in clean_prefixes)
        country_rows.append((country, _common_prefix(clean_prefixes), len(clean_prefixes)))

    conn.execute("DELETE FROM e164_prefixes")
    conn.execute("DELETE FROM e164_countries")
    conn.executemany(
        "INSERT OR REPLACE INTO e164_prefixes (prefix, country) VALUES (?, ?)",
        prefix_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO e164_countries (country, primary_prefix, prefix_count) VALUES (?, ?, ?)",
        country_rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('e164_data_version', ?)",
        (version,),
    )


def resolve_e164(conn, destination):
    """Return the country for a destination by longest E.164 prefix match."""
    digits = normalize_phone_number(destination)
    max_len = min(len(digits), 15)
    for length in range(max_len, 0, -1):
        prefix = digits[:length]
        row = conn.execute(
            "SELECT p.prefix, p.country, c.primary_prefix, c.prefix_count "
            "FROM e164_prefixes p LEFT JOIN e164_countries c ON c.country = p.country "
            "WHERE p.prefix = ?",
            (prefix,),
        ).fetchone()
        if row is not None:
            return row
    return None


def get_e164_country(conn, country):
    country = canonical_direction_name(country)
    if not country:
        return None
    return conn.execute(
        "SELECT * FROM e164_countries WHERE country = ?",
        (country,),
    ).fetchone()


def direction_matches(country_a, country_b):
    return canonical_direction_name(country_a) == canonical_direction_name(country_b)


def is_countrywide_prefix(conn, country, prefix):
    row = get_e164_country(conn, country)
    if row is None:
        return False
    return normalize_phone_number(prefix) == row["primary_prefix"]


def destination_matches_route(conn, destination, prefix, destination_name):
    destination = normalize_phone_number(destination)
    prefix = normalize_phone_number(prefix)
    if prefix and destination.startswith(prefix):
        return True
    resolved = resolve_e164(conn, destination)
    if resolved is None:
        return False
    return (
        direction_matches(destination_name, resolved["country"])
        and is_countrywide_prefix(conn, destination_name, prefix)
    )


def list_e164_countries(conn):
    rows = conn.execute(
        "SELECT country, primary_prefix, prefix_count "
        "FROM e164_countries ORDER BY country"
    ).fetchall()
    return [dict(row) for row in rows]


def get_client_by_ip(conn, sip_ip):
    candidates = split_ip_list(sip_ip)
    if not candidates and (sip_ip or "").strip():
        candidates = [(sip_ip or "").strip()]

    for candidate in candidates:
        row = conn.execute("SELECT * FROM clients WHERE sip_ip = ?", (candidate,)).fetchone()
        if row is not None:
            return row

    rows = conn.execute("SELECT * FROM clients").fetchall()
    for row in rows:
        tokens = split_ip_list(row["sip_ip"])
        for candidate in candidates:
            if any(ip_token_matches(candidate, token) for token in tokens):
                return row
    return None


def match_client_rate(conn, client_id, destination):
    """Тариф клиента по самому длинному подходящему префиксу."""
    match = match_client_rate_for_destination(conn, client_id, destination)
    return match[0] if match is not None else None


def _pick_client_rate_candidate(candidates, seed):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    idx = int(hashlib.sha256(str(seed or "").encode()).hexdigest(), 16) % len(candidates)
    return candidates[idx]


def match_client_rate_for_destination(conn, client_id, destination, seed=""):
    """Тариф клиента + номер без входящего клиентского техпрефикса."""
    destination = normalize_phone_number(destination)
    rows = conn.execute(
        "SELECT * FROM client_rates WHERE client_id = ? "
        "ORDER BY length(client_tech_prefix) DESC, length(prefix) DESC, id",
        (client_id,),
    ).fetchall()
    candidates = []
    best_key = None
    for r in rows:
        client_tech_prefix = (r["client_tech_prefix"] or "") if "client_tech_prefix" in r.keys() else ""
        routed_destination = destination
        if client_tech_prefix:
            if not destination.startswith(client_tech_prefix):
                continue
            routed_destination = destination[len(client_tech_prefix):]
            if not routed_destination:
                continue
        if destination_matches_route(conn, routed_destination, r["prefix"], r["destination_name"]):
            key = (len(client_tech_prefix), len(r["prefix"] or ""))
            if best_key is None:
                best_key = key
            elif key != best_key:
                break
            candidates.append((r, routed_destination, client_tech_prefix))
    return _pick_client_rate_candidate(candidates, f"{client_id}:{destination}:{seed}")


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
        if destination_matches_route(conn, destination, r["prefix"], r["destination_name"]):
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


SIP_HIT_COLUMNS = (
    "call_uuid",
    "sip_ip",
    "sip_port",
    "clid",
    "destination",
    "client_id",
    "client_name",
    "client_tech_prefix",
    "dial_destination",
    "provider_number",
    "gateway_name",
    "route_ip",
    "terminator_id",
    "terminator_name",
    "terminator_destination_name",
    "terminator_prefix",
    "status",
    "stage",
    "reason",
    "max_seconds",
    "sell_rate_cents",
    "cost_rate_cents",
    "sell_billing_cycle",
    "cost_billing_cycle",
    "user_agent",
    "sip_call_id",
    "profile",
    "context",
)


def record_sip_hit(fields):
    """Write one incoming SIP attempt as seen by the billing gate."""
    conn = get_conn()
    try:
        values = []
        for col in SIP_HIT_COLUMNS:
            value = fields.get(col)
            if value is None and col not in {"client_id", "terminator_id", "max_seconds"}:
                value = ""
            values.append(value)
        placeholders = ", ".join("?" for _ in SIP_HIT_COLUMNS)
        conn.execute(
            f"INSERT INTO sip_hits ({', '.join(SIP_HIT_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    finally:
        conn.close()


PCAP_EVENT_COLUMNS = (
    "observed_at",
    "direction",
    "src_ip",
    "src_port",
    "dst_ip",
    "dst_port",
    "method",
    "status_code",
    "status_text",
    "call_id",
    "cseq",
    "from_user",
    "to_user",
    "request_uri",
    "user_agent",
    "reason",
    "raw_summary",
)


def record_pcap_events(events, keep_latest=3000):
    """Persist parsed SIP packets from the passive FreeSWITCH packet collector."""
    if not events:
        return 0
    conn = get_conn()
    try:
        values = []
        for event in events:
            row = []
            for col in PCAP_EVENT_COLUMNS:
                value = event.get(col)
                if value is None and col != "status_code":
                    value = ""
                row.append(value)
            values.append(row)
        placeholders = ", ".join("?" for _ in PCAP_EVENT_COLUMNS)
        conn.executemany(
            f"INSERT INTO pcap_events ({', '.join(PCAP_EVENT_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        conn.execute(
            "DELETE FROM pcap_events WHERE id NOT IN "
            "(SELECT id FROM pcap_events ORDER BY id DESC LIMIT ?)",
            (keep_latest,),
        )
        conn.commit()
        return len(values)
    finally:
        conn.close()
