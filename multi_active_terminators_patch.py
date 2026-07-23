from fastapi import HTTPException


def _remove_routes(app, path, methods=None):
    wanted = set(methods or [])
    kept = []
    for route in app.router.routes:
        route_path = getattr(route, "path", "")
        route_methods = set(getattr(route, "methods", set()) or set())
        if route_path == path and (not wanted or route_methods & wanted):
            continue
        kept.append(route)
    app.router.routes = kept


def _model_fields(data):
    values = data.model_dump() if hasattr(data, "model_dump") else data.dict()
    return {key: value for key, value in values.items() if value is not None}


def _deactivate_same_tech(conn, *, prefix, tech_prefix, exclude_id=None):
    sql = "UPDATE terminators SET active = 0 WHERE prefix = ? AND COALESCE(tech_prefix, '') = ?"
    params = [prefix, tech_prefix or ""]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    conn.execute(sql, params)


def install(app, main, db):
    _remove_routes(app, "/api/terminators", {"POST"})
    _remove_routes(app, "/api/terminators/{tid}", {"PATCH"})
    _remove_routes(app, "/api/terminators/{tid}/activate", {"POST"})

    @app.post("/api/terminators", dependencies=main.ADMIN_AUTH)
    def create_terminator(data: main.TerminatorIn):
        conn = db.get_conn()
        try:
            group = db.get_termination_group(conn, data.gateway_group_id)
            if data.gateway_group_id is not None and group is None:
                raise HTTPException(404, "Терминационная группа не найдена")
            if group is None and (not data.gateway_name.strip()) and (not db.split_ip_list(data.ips)):
                raise HTTPException(400, "Укажите gateway или IP терминатора")
            conn.execute("BEGIN IMMEDIATE")
            if data.active:
                _deactivate_same_tech(conn, prefix=data.prefix, tech_prefix=data.tech_prefix)
            cur = conn.execute(
                "INSERT INTO terminators "
                "(name, gateway_group_id, ips, destination_name, prefix, gateway_name, tech_prefix, cost_rate_cents, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data.name,
                    data.gateway_group_id,
                    data.ips,
                    data.destination_name,
                    data.prefix,
                    data.gateway_name,
                    data.tech_prefix,
                    data.cost_rate_cents,
                    int(data.active),
                ),
            )
            conn.commit()
            return {"id": cur.lastrowid}
        finally:
            conn.close()

    @app.patch("/api/terminators/{tid}", dependencies=main.ADMIN_AUTH)
    def update_terminator(tid: int, data: main.TerminatorUpdateIn):
        fields = _model_fields(data)
        if not fields:
            raise HTTPException(400, "Нет полей для обновления")
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM terminators WHERE id = ?", (tid,)).fetchone()
            if row is None:
                conn.rollback()
                raise HTTPException(404, "Терминатор не найден")
            next_group_id = fields.get("gateway_group_id", row["gateway_group_id"] if "gateway_group_id" in row.keys() else None)
            group = db.get_termination_group(conn, next_group_id)
            if next_group_id is not None and group is None:
                conn.rollback()
                raise HTTPException(404, "Терминационная группа не найдена")
            next_gateway = fields.get("gateway_name", row["gateway_name"]) or ""
            next_ips = fields.get("ips", row["ips"]) or ""
            if group is None and (not next_gateway.strip()) and (not db.split_ip_list(next_ips)):
                conn.rollback()
                raise HTTPException(400, "Укажите gateway или IP терминатора")

            will_be_active = bool(fields.get("active", row["active"]))
            next_prefix = fields.get("prefix", row["prefix"])
            next_tech_prefix = fields.get("tech_prefix", row["tech_prefix"] if "tech_prefix" in row.keys() else "") or ""
            if will_be_active:
                _deactivate_same_tech(conn, prefix=next_prefix, tech_prefix=next_tech_prefix, exclude_id=tid)
            if "active" in fields:
                fields["active"] = int(fields["active"])
            sets = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(f"UPDATE terminators SET {sets} WHERE id = ?", (*fields.values(), tid))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @app.post("/api/terminators/{tid}/activate", dependencies=main.ADMIN_AUTH)
    def activate_terminator(tid: int):
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM terminators WHERE id = ?", (tid,)).fetchone()
            if row is None:
                conn.rollback()
                raise HTTPException(404, "Терминатор не найден")
            _deactivate_same_tech(
                conn,
                prefix=row["prefix"],
                tech_prefix=row["tech_prefix"] if "tech_prefix" in row.keys() else "",
                exclude_id=tid,
            )
            conn.execute("UPDATE terminators SET active = 1 WHERE id = ?", (tid,))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()
