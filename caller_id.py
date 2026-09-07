"""Per-route outbound caller IDs. Selection runs inside the reserve transaction."""
import re


def init_schema(conn):
    additions = {
        "client_rates": {
            "caller_id_pool": "TEXT NOT NULL DEFAULT ''",
            "caller_id_cursor": "INTEGER NOT NULL DEFAULT 0",
        },
        "reservations": {
            "outbound_caller_id": "TEXT NOT NULL DEFAULT ''",
            "caller_id_rate_id": "INTEGER",
        },
    }
    for table, columns in additions.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def normalize_pool(text):
    numbers = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", token):
            raise ValueError("Caller ID: нужен международный номер, 7–15 цифр, можно с +")
        number = token.lstrip("+")
        if number not in numbers:
            numbers.append(number)
    if len(numbers) > 200:
        raise ValueError("Caller ID: максимум 200 номеров")
    return "\n".join(numbers)


def save_pool(conn, rate_id, text):
    pool = normalize_pool(text)
    return conn.execute(
        "UPDATE client_rates SET caller_id_cursor = CASE WHEN caller_id_pool = ? "
        "THEN caller_id_cursor ELSE 0 END, caller_id_pool = ? WHERE id = ?",
        (pool, pool, rate_id),
    ).rowcount


def select_number(conn, rate, call_uuid, client_id):
    pool = rate["caller_id_pool"]
    if not pool:
        return ""
    previous = conn.execute(
        "SELECT outbound_caller_id FROM reservations "
        "WHERE call_uuid = ? AND client_id = ? AND caller_id_rate_id = ?",
        (call_uuid, client_id, rate["id"]),
    ).fetchone()
    if previous and previous["outbound_caller_id"]:
        return previous["outbound_caller_id"]
    numbers = pool.splitlines()
    index = rate["caller_id_cursor"] % len(numbers)
    conn.execute(
        "UPDATE client_rates SET caller_id_cursor = ? WHERE id = ?",
        ((index + 1) % len(numbers), rate["id"]),
    )
    return numbers[index]
