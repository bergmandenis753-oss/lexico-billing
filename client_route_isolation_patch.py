from fastapi import HTTPException


def _remove_routes(app, path, methods=None):
    wanted = {m.upper() for m in methods} if methods else None
    kept = []
    for route in app.router.routes:
        route_path = getattr(route, "path", "")
        route_methods = set(getattr(route, "methods", set()) or set())
        if route_path == path and (wanted is None or route_methods & wanted):
            continue
        kept.append(route)
    app.router.routes = kept


def _dump(data):
    return data.model_dump() if hasattr(data, "model_dump") else data.dict()


def _clean(value):
    return str(value or "").strip()


def _validate_client_route(conn, main, db, data):
    client_id = int(data["client_id"])
    terminator_id = data.get("terminator_id")
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        raise HTTPException(404, "Оригинатор не найден")
    if not client["active"]:
        raise HTTPException(400, "Оригинатор выключен")
    route = None
    if terminator_id is not None:
        route = db.get_terminator(conn, int(terminator_id))
        if route is None:
            raise HTTPException(404, "Терминатор не найден")
        if not route["active"]:
            raise HTTPException(400, "Терминатор выключен")
        group = db.get_termination_group(
            conn,
            route["gateway_group_id"] if "gateway_group_id" in route.keys() else None,
        )
        gateway_name = (route["gateway_name"] or "").strip()
        route_ips = route["ips"] or ""
        if group is not None:
            gateway_name = gateway_name or (group["gateway_name"] or "").strip()
            route_ips = route_ips or group["ips"] or ""
        if not gateway_name and not db.split_ip_list(route_ips):
            raise HTTPException(400, "У терминатора нет gateway/IP")
    return client, route


def _drop_unsafe_client_rate_indexes(conn):
    """Keep only indexes that do not make routes global across originators."""
    for idx in conn.execute("PRAGMA index_list(client_rates)").fetchall():
        if not idx["unique"]:
            continue
        idx_name = idx["name"]
        safe_name = idx_name.replace('"', '""')
        idx_cols = [r["name"] for r in conn.execute(f'PRAGMA index_info("{safe_name}")').fetchall()]
        if idx_cols != ["client_id", "client_tech_prefix", "prefix"]:
            conn.execute(f'DROP INDEX IF EXISTS "{safe_name}"')


def _upsert_client_route(conn, data, *, update_existing=True):
    client_id = int(data["client_id"])
    tech = _clean(data.get("client_tech_prefix"))
    prefix = _clean(data.get("prefix"))
    dest = _clean(data.get("destination_name"))
    if not prefix or not dest:
        raise HTTPException(400, "Укажите направление и префикс")
    existing = conn.execute(
        "SELECT * FROM client_rates "
        "WHERE client_id = ? AND COALESCE(client_tech_prefix, '') = ? AND prefix = ? "
        "ORDER BY id LIMIT 1",
        (client_id, tech, prefix),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            "INSERT INTO client_rates "
            "(client_id, terminator_id, client_tech_prefix, prefix, destination_name, sell_rate_cents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, data.get("terminator_id"), tech, prefix, dest, int(data["sell_rate_cents"])),
        )
        return cur.lastrowid, True
    if not update_existing:
        raise HTTPException(409, "Такое направление у этого оригинатора уже есть")
    conn.execute(
        "UPDATE client_rates SET terminator_id = ?, destination_name = ?, sell_rate_cents = ? "
        "WHERE id = ?",
        (data.get("terminator_id"), dest, int(data["sell_rate_cents"]), existing["id"]),
    )
    return existing["id"], False


def install(app, main, db):
    _remove_routes(app, "/api/client-rates", {"POST"})
    _remove_routes(app, "/api/directions", {"POST"})

    conn = db.get_conn()
    try:
        _drop_unsafe_client_rate_indexes(conn)
        conn.commit()
    finally:
        conn.close()

    @app.get("/api/ops/route-isolation-status", dependencies=main.API_AUTH)
    def route_isolation_status():
        conn = db.get_conn()
        try:
            _drop_unsafe_client_rate_indexes(conn)
            conn.commit()
            indexes = []
            for idx in conn.execute("PRAGMA index_list(client_rates)").fetchall():
                idx_name = idx["name"]
                safe_name = idx_name.replace('"', '""')
                indexes.append(
                    {
                        "name": idx_name,
                        "unique": bool(idx["unique"]),
                        "columns": [
                            row["name"]
                            for row in conn.execute(f'PRAGMA index_info("{safe_name}")').fetchall()
                        ],
                    }
                )
            routes = [
                dict(row)
                for row in conn.execute(
                    "SELECT cr.id, c.name AS client_name, COALESCE(cr.client_tech_prefix, '') AS client_tech_prefix, "
                    "cr.prefix, cr.destination_name, t.name AS terminator_name "
                    "FROM client_rates cr JOIN clients c ON c.id = cr.client_id "
                    "LEFT JOIN terminators t ON t.id = cr.terminator_id "
                    "ORDER BY cr.id DESC LIMIT 100"
                ).fetchall()
            ]
            return {
                "ok": True,
                "route_key": "client_id + client_tech_prefix + prefix",
                "indexes": indexes,
                "routes": routes,
            }
        finally:
            conn.close()

    @app.post("/api/client-rates", dependencies=main.ADMIN_AUTH)
    def create_client_rate(data: main.ClientRateIn):
        payload = _dump(data)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _validate_client_route(conn, main, db, payload)
            rate_id, created = _upsert_client_route(conn, payload, update_existing=True)
            conn.commit()
            return {"id": rate_id, "created": created}
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/api/directions", dependencies=main.ADMIN_AUTH)
    def create_direction(data: main.DirectionIn):
        payload = _dump(data)
        payload["client_tech_prefix"] = _clean(payload.get("client_tech_prefix"))
        payload["prefix"] = _clean(payload.get("prefix"))
        payload["destination_name"] = _clean(payload.get("destination_name"))
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            client, _route = _validate_client_route(conn, main, db, payload)
            country = db.get_e164_country(conn, payload["destination_name"])
            if country is not None and payload["prefix"] != country["primary_prefix"]:
                raise HTTPException(
                    400,
                    f'Для {country["country"]} используйте основной E.164 префикс {country["primary_prefix"]}, тогда покроются все подпрефиксы страны',
                )
            rate_id, created = _upsert_client_route(
                conn,
                payload,
                update_existing=bool(payload.get("update_existing", True)),
            )
            check_number = f"{payload.get('client_tech_prefix') or ''}{payload['prefix']}5551234"
            preview = main._route_preview(
                conn,
                client=client,
                destination=check_number,
                terminator_id=payload.get("terminator_id"),
            )
            conn.commit()
            return {"ok": True, "created": created, "client_rate_id": rate_id, "preview": preview}
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()
