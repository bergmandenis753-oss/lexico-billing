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


def install(app, main, db):
    _remove_routes(app, "/api/terminators", {"POST"})
    _remove_routes(app, "/api/terminators/{tid}", {"PATCH"})
    _remove_routes(app, "/api/terminators/{tid}/activate", {"POST"})

    @app.post("/api/terminators", dependencies=main.ADMIN_AUTH)
    def create_terminator(data: main.TerminatorIn):
        conn = db.get_conn()
        try:
            raw_mode = str(data.route_mode or "sip").strip().lower()
            if raw_mode not in db.TERMINATOR_MODES:
                raise HTTPException(400, "Неизвестный тип терминатора")
            route_mode = db.normalize_terminator_mode(raw_mode)
            audio_asset = db.normalize_audio_asset(data.audio_asset)
            if route_mode == "audio" and not audio_asset:
                raise HTTPException(400, "Для Audio/IVR укажите WAV-файл, например greeting.wav")

            group_id = None if route_mode == "audio" else data.gateway_group_id
            ips = "" if route_mode == "audio" else data.ips
            gateway_name = "" if route_mode == "audio" else data.gateway_name
            tech_prefix = "" if route_mode == "audio" else data.tech_prefix
            cost_rate_cents = 0 if route_mode == "audio" else data.cost_rate_cents

            group = db.get_termination_group(conn, group_id)
            if group_id is not None and group is None:
                raise HTTPException(404, "Терминационная группа не найдена")
            if route_mode == "sip" and group is None and (not gateway_name.strip()) and (not db.split_ip_list(ips)):
                raise HTTPException(400, "Укажите gateway или IP терминатора")
            billing_cycle = db.normalize_billing_cycle(
                data.billing_cycle or db.default_billing_cycle_for_route(data.destination_name, data.prefix)
            )
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO terminators "
                "(name, gateway_group_id, ips, destination_name, prefix, gateway_name, tech_prefix, "
                "cost_rate_cents, billing_cycle, route_mode, audio_asset, audio_max_seconds, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data.name,
                    group_id,
                    ips,
                    data.destination_name,
                    data.prefix,
                    gateway_name,
                    tech_prefix,
                    cost_rate_cents,
                    billing_cycle,
                    route_mode,
                    audio_asset if route_mode == "audio" else "",
                    data.audio_max_seconds,
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
        if "billing_cycle" in fields:
            fields["billing_cycle"] = db.normalize_billing_cycle(fields["billing_cycle"])
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM terminators WHERE id = ?", (tid,)).fetchone()
            if row is None:
                conn.rollback()
                raise HTTPException(404, "Терминатор не найден")

            raw_mode = str(
                fields.get("route_mode", row["route_mode"] if "route_mode" in row.keys() else "sip") or "sip"
            ).strip().lower()
            if raw_mode not in db.TERMINATOR_MODES:
                conn.rollback()
                raise HTTPException(400, "Неизвестный тип терминатора")
            next_mode = db.normalize_terminator_mode(raw_mode)
            fields["route_mode"] = next_mode
            next_audio_asset = db.normalize_audio_asset(
                fields.get("audio_asset", row["audio_asset"] if "audio_asset" in row.keys() else "")
            )
            if next_mode == "audio":
                if not next_audio_asset:
                    conn.rollback()
                    raise HTTPException(400, "Для Audio/IVR укажите WAV-файл, например greeting.wav")
                fields.update(
                    {
                        "gateway_group_id": None,
                        "ips": "",
                        "gateway_name": "",
                        "tech_prefix": "",
                        "cost_rate_cents": 0,
                        "audio_asset": next_audio_asset,
                    }
                )
            else:
                fields["audio_asset"] = ""

            next_group_id = fields.get("gateway_group_id", row["gateway_group_id"] if "gateway_group_id" in row.keys() else None)
            group = db.get_termination_group(conn, next_group_id)
            if next_group_id is not None and group is None:
                conn.rollback()
                raise HTTPException(404, "Терминационная группа не найдена")
            next_gateway = fields.get("gateway_name", row["gateway_name"]) or ""
            next_ips = fields.get("ips", row["ips"]) or ""
            if next_mode == "sip" and group is None and (not next_gateway.strip()) and (not db.split_ip_list(next_ips)):
                conn.rollback()
                raise HTTPException(400, "Укажите gateway или IP терминатора")

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
            conn.execute("UPDATE terminators SET active = 1 WHERE id = ?", (tid,))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()
