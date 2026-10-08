"""Per-client-route simultaneous call limits."""


MAX_LINE_LIMIT = 10000


def init_schema(conn):
    rate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(client_rates)")}
    rate_additions = {
        "line_limit_enabled": "INTEGER NOT NULL DEFAULT 0",
        "line_limit": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in rate_additions.items():
        if name not in rate_columns:
            conn.execute(f"ALTER TABLE client_rates ADD COLUMN {name} {definition}")

    reservation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(reservations)")}
    if "client_rate_id" not in reservation_columns:
        conn.execute("ALTER TABLE reservations ADD COLUMN client_rate_id INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resv_client_rate_expiry "
        "ON reservations(client_rate_id, expires_at)"
    )


def normalize(enabled, limit):
    enabled = bool(enabled)
    try:
        limit = int(limit or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Лимит линий должен быть целым числом") from exc
    if limit < 0 or limit > MAX_LINE_LIMIT:
        raise ValueError(f"Лимит линий должен быть от 1 до {MAX_LINE_LIMIT}")
    if enabled and limit < 1:
        raise ValueError("Укажите лимит минимум 1 линия или выключите ограничение")
    return int(enabled), limit


def config(rate):
    enabled = "line_limit_enabled" in rate.keys() and bool(rate["line_limit_enabled"])
    limit = int(rate["line_limit"] or 0) if "line_limit" in rate.keys() else 0
    return enabled, limit


def active_count(conn, rate_id, now_ts, exclude_call_uuid=""):
    query = (
        "SELECT COUNT(*) AS c FROM reservations "
        "WHERE client_rate_id = ? AND expires_at > ?"
    )
    params = [rate_id, now_ts]
    if exclude_call_uuid:
        query += " AND call_uuid <> ?"
        params.append(exclude_call_uuid)
    row = conn.execute(query, params).fetchone()
    return int(row["c"] or 0)


def check(conn, rate, now_ts, call_uuid=""):
    enabled, limit = config(rate)
    if not enabled:
        return True, 0, limit
    active = active_count(conn, rate["id"], now_ts, call_uuid)
    return active < limit, active, limit
