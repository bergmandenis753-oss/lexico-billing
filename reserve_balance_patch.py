import math
import sqlite3

import route_line_limit
import voice_nodes

from fastapi import HTTPException


def _remove_routes(app, path, methods=None):
    wanted = {m.upper() for m in methods} if methods else None
    app.router.routes = [
        route for route in app.router.routes
        if not (
            getattr(route, "path", "") == path
            and (wanted is None or set(getattr(route, "methods", set()) or set()) & wanted)
        )
    ]


def _active_call_count(conn, client_id, now_ts, exclude_call_uuid=""):
    query = "SELECT COUNT(*) AS c FROM reservations WHERE client_id = ? AND expires_at > ?"
    params = [client_id, now_ts]
    if exclude_call_uuid:
        query += " AND call_uuid <> ?"
        params.append(exclude_call_uuid)
    row = conn.execute(query, params).fetchone()
    return int(row["c"] or 0)


def install(app, main, db):
    _remove_routes(app, "/api/reserve", {"POST"})

    @app.post("/api/reserve", dependencies=main.API_AUTH)
    def reserve(data: main.ReserveIn):
        conn = db.get_conn()
        client = rate = route = None
        stage = "received"
        client_tech_prefix = dial_destination = gateway_name = route_ip = provider_number = ""
        max_seconds = None
        try:
            now_ts = db.now()
            stage = "client_lookup"
            client = db.get_client_by_ip(conn, data.sip_ip)
            if client is None:
                raise HTTPException(403, f"Клиент с IP {data.sip_ip} не найден")
            stage = "client_status"
            if not client["active"]:
                raise HTTPException(403, "Клиент неактивен")
            stage = "client_rate"
            route_seed = (
                data.call_uuid
                or data.sip_call_id
                or f"{data.sip_ip}:{data.sip_port}:{data.clid}:{data.destination}:{now_ts}"
            )
            incoming_node = voice_nodes.normalize(data.voice_node)
            rate_match = db.match_client_rate_for_destination(
                conn, client["id"], data.destination, route_seed, incoming_node
            )
            if rate_match is None:
                other_node_match = db.match_client_rate_for_destination(
                    conn, client["id"], data.destination, route_seed
                )
                if other_node_match is not None:
                    stage = "voice_node"
                    assigned_node = voice_nodes.normalize(other_node_match[0]["voice_node"])
                    raise HTTPException(
                        503,
                        f"Роут назначен серверу {assigned_node}, звонок пришёл на {incoming_node}",
                    )
                raise HTTPException(403, f"Нет тарифа клиента для {data.destination}")
            rate, dial_destination, client_tech_prefix = rate_match
            call_uuid = data.call_uuid or f"nouuid-{now_ts}-{client['id']}"
            if rate["sell_rate_cents"] <= 0:
                raise HTTPException(403, "Некорректный тариф продажи (<= 0)")
            stage = "terminator"
            route = db.get_terminator(conn, rate["terminator_id"]) if "terminator_id" in rate.keys() else None
            if route is None:
                route = db.match_active_terminator(conn, dial_destination)
            if route is None:
                raise HTTPException(403, f"Нет терминатора для {dial_destination}")
            group = db.get_termination_group(conn, route["gateway_group_id"]) if "gateway_group_id" in route.keys() else None
            gateway_name = (route["gateway_name"] or "").strip()
            route_ips = route["ips"] or ""
            if group is not None:
                gateway_name = gateway_name or (group["gateway_name"] or "").strip()
                route_ips = route_ips or group["ips"] or ""
            route_ip = db.pick_ip(route_ips, data.call_uuid or dial_destination)
            stage = "gateway"
            if not gateway_name and not route_ip:
                raise HTTPException(403, "У терминатора не указан ни gateway, ни IP")

            provider_number = f"{route['tech_prefix'] or ''}{dial_destination}"

            # Keep the single-writer section to the balance/limit check and one
            # reservation insert. Route discovery above is read-only and must
            # not block unrelated calls.
            stage = "balance"
            conn.execute("BEGIN IMMEDIATE")
            db.cleanup_expired(conn, now_ts)
            current_client = conn.execute(
                "SELECT * FROM clients WHERE id = ?", (client["id"],)
            ).fetchone()
            if current_client is None or not current_client["active"]:
                raise HTTPException(403, "Клиент неактивен")
            client = current_client
            line_allowed, route_active_calls, route_line_limit_value = route_line_limit.check(
                conn, rate, now_ts, call_uuid
            )
            if not line_allowed:
                stage = "route_line_limit"
                raise HTTPException(
                    503,
                    f"Лимит линий роута исчерпан: {route_active_calls}/{route_line_limit_value}",
                )
            active_calls = _active_call_count(conn, client["id"], now_ts, call_uuid)
            if client["balance_cents"] <= 0:
                raise HTTPException(403, "Недостаточно средств")
            per_call_balance = math.floor(client["balance_cents"] / (active_calls + 1))
            max_seconds = math.floor(per_call_balance / rate["sell_rate_cents"] * 60)
            if max_seconds <= 0:
                raise HTTPException(403, "Недостаточно средств даже на 1 секунду разговора")

            reserved = math.ceil(max_seconds * rate["sell_rate_cents"] / 60)
            expires_at = now_ts + max_seconds + db.RESERVATION_BUFFER_SEC
            conn.execute(
                "INSERT INTO reservations "
                "(client_id, client_rate_id, call_uuid, reserved_cents, expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(call_uuid) DO UPDATE SET "
                "client_id = excluded.client_id, client_rate_id = excluded.client_rate_id, "
                "reserved_cents = excluded.reserved_cents, expires_at = excluded.expires_at",
                (client["id"], rate["id"], call_uuid, reserved, expires_at),
            )
            stage = "reserved"
            reason = f"Пропущен; активных звонков={active_calls}; лимит={max_seconds} сек"
            main._safe_record_sip_hit(
                data, status_text="allowed", stage=stage, reason=reason, client=client, rate=rate, route=route,
                gateway_name=gateway_name, route_ip=route_ip, dial_destination=dial_destination,
                provider_number=provider_number, client_tech_prefix=client_tech_prefix, max_seconds=max_seconds,
                conn=conn,
            )
            conn.commit()
            return {
                "allowed": True, "max_seconds": max_seconds,
                "sell_rate_cents": rate["sell_rate_cents"], "cost_rate_cents": route["cost_rate_cents"],
                "gateway_name": gateway_name, "route_ip": route_ip, "tech_prefix": route["tech_prefix"],
                "client_tech_prefix": client_tech_prefix, "dial_destination": dial_destination,
                "provider_number": provider_number, "terminator_id": route["id"], "terminator_name": route["name"],
                "terminator_destination_name": route["destination_name"], "terminator_prefix": route["prefix"],
                "terminator_tech_prefix": route["tech_prefix"], "voice_node": incoming_node,
                "client_id": client["id"], "call_uuid": call_uuid,
            }
        except HTTPException as exc:
            conn.rollback()
            main._safe_record_sip_hit(
                data, status_text="rejected", stage=stage, reason=str(exc.detail), client=client, rate=rate, route=route,
                gateway_name=gateway_name, route_ip=route_ip, dial_destination=dial_destination,
                provider_number=provider_number, client_tech_prefix=client_tech_prefix, max_seconds=max_seconds,
            )
            raise
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            print(f"[RESERVE BUSY] call={data.call_uuid}: {exc}")
            raise HTTPException(503, "База временно занята, повторите запрос") from exc
        finally:
            conn.close()
