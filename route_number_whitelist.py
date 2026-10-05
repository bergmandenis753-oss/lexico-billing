"""Per-route allowlists for inbound A and B numbers."""

import re


KINDS = {
    "a": ("a_number_whitelist_enabled", "a_number_whitelist"),
    "b": ("b_number_whitelist_enabled", "b_number_whitelist"),
}


def init_schema(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(client_rates)")}
    additions = {
        "a_number_whitelist_enabled": "INTEGER NOT NULL DEFAULT 0",
        "a_number_whitelist": "TEXT NOT NULL DEFAULT ''",
        "b_number_whitelist_enabled": "INTEGER NOT NULL DEFAULT 0",
        "b_number_whitelist": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE client_rates ADD COLUMN {name} {definition}")


def normalize_number(value):
    raw = str(value or "").strip()
    if not re.fullmatch(r"\+?[0-9][0-9\s().-]*", raw):
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def normalize_list(text):
    numbers = []
    for entry in re.split(r"[\n,;]+", str(text or "").strip()):
        entry = entry.strip()
        if not entry:
            continue
        if not re.fullmatch(r"\+?[0-9][0-9\s().-]*", entry):
            raise ValueError("Whitelist: по одному международному номеру в строке")
        number = normalize_number(entry)
        if not re.fullmatch(r"[1-9][0-9]{6,14}", number):
            raise ValueError("Whitelist: номер должен содержать 7-15 цифр и не начинаться с 0")
        if number not in numbers:
            numbers.append(number)
    if len(numbers) > 1000:
        raise ValueError("Whitelist: максимум 1000 номеров")
    return "\n".join(numbers)


def save(conn, rate_id, kind, enabled, text):
    try:
        enabled_column, list_column = KINDS[kind]
    except KeyError as exc:
        raise ValueError("Неизвестный тип whitelist") from exc
    numbers = normalize_list(text)
    cursor = conn.execute(
        f"UPDATE client_rates SET {enabled_column} = ?, {list_column} = ? WHERE id = ?",
        (int(bool(enabled)), numbers, rate_id),
    )
    return cursor.rowcount, numbers


def denied_kind(rate, a_number, b_number):
    for kind, raw_number in (("a", a_number), ("b", b_number)):
        enabled_column, list_column = KINDS[kind]
        if enabled_column not in rate.keys() or not rate[enabled_column]:
            continue
        allowed = set(str(rate[list_column] or "").splitlines())
        if normalize_number(raw_number) not in allowed:
            return kind
    return None
