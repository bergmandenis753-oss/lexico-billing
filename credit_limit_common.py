def remove_routes(app, path, methods=None):
    wanted = {method.upper() for method in methods} if methods else None
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", "") == path
            and (wanted is None or set(getattr(route, "methods", set()) or set()) & wanted)
        )
    ]


def no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def uses_current_money_scale(request, db) -> bool:
    return request.headers.get("x-money-scale", "").strip() == str(db.MONEY_SCALE)


def legacy_money(db, value):
    if value is None:
        return value
    return value / db.LEGACY_CENT_TO_MONEY_UNITS


def legacy_rows(db, rows, fields):
    out = []
    for row in rows:
        item = dict(row)
        for field in fields:
            if field in item:
                item[field] = legacy_money(db, item[field])
        out.append(item)
    return out


def rows_as_dict(rows):
    return [dict(row) for row in rows]


def ensure_schema(db):
    db.init_db()
    conn = db.get_conn()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
        if "credit_limit_cents" not in columns:
            conn.execute("ALTER TABLE clients ADD COLUMN credit_limit_cents INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def client_credit_limit_units(client) -> int:
    if client is None:
        return 0
    try:
        value = client["credit_limit_cents"] if "credit_limit_cents" in client.keys() else 0
    except AttributeError:
        value = dict(client).get("credit_limit_cents", 0)
    return max(0, int(value or 0))


def minimum_client_balance_units(client) -> int:
    return -client_credit_limit_units(client)


def available_client_units(client, held_units=0) -> int:
    return int(client["balance_cents"] or 0) + client_credit_limit_units(client) - int(held_units or 0)


def max_charge_units_for_client(client) -> int:
    return max(0, int(client["balance_cents"] or 0) - minimum_client_balance_units(client))


def client_row(row):
    item = dict(row)
    credit_limit = max(0, int(item.get("credit_limit_cents") or 0))
    balance = int(item.get("balance_cents") or 0)
    item["credit_limit_cents"] = credit_limit
    item["minimum_balance_cents"] = -credit_limit
    item["available_balance_cents"] = balance + credit_limit
    return item


def active_call_count(conn, client_id, now_ts):
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM reservations WHERE client_id = ? AND expires_at > ?",
        (client_id, now_ts),
    ).fetchone()
    return int(row["c"] or 0)
