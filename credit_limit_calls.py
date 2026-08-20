import math
import re

from fastapi import HTTPException
from pydantic import BaseModel

from credit_limit_common import (
    active_call_count,
    available_client_units,
    client_credit_limit_units,
    ensure_schema,
    max_charge_units_for_client,
    minimum_client_balance_units,
)


class OpsBalanceAdjustIn(BaseModel):
    client_id: int
    amount_cents: int
    reason: str = ""


def _clean_lookup_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _clean_route_ip(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("direct:"):
        text = text.split(":", 1)[1]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            text = host
    return text.strip()


def _resolve_termination_group(conn, db, data):
    hit = None
    if getattr(data, "call_uuid", ""):
        hit = conn.execute(
            "SELECT route_ip, gateway_name, terminator_id, terminator_name "
            "FROM sip_hits WHERE call_uuid = ? ORDER BY rowid DESC LIMIT 1",
            (data.call_uuid,),
        ).fetchone()

    terminator_id = getattr(data, "terminator_id", None)
    if terminator_id is None and hit is not None:
        terminator_id = hit["terminator_id"]
    if terminator_id is not None:
        row = conn.execute(
            "SELECT g.id, g.name FROM terminators t "
            "JOIN termination_groups g ON g.id = t.gateway_group_id "
            "WHERE t.id = ? LIMIT 1",
            (terminator_id,),
        ).fetchone()
        if row is not None:
            return row

    names = []
    for value in (
        getattr(data, "terminator_name", ""),
        hit["terminator_name"] if hit is not None else "",
        getattr(data, "gateway_name", ""),
        hit["gateway_name"] if hit is not None else "",
    ):
        name = _clean_lookup_text(value)
        if name and name not in {"direct", "direct ip", "manual"} and name not in names:
            names.append(name)
    for name in names:
        row = conn.execute(
            "SELECT id, name FROM termination_groups "
            "WHERE lower(trim(name)) = ? OR lower(trim(gateway_name)) = ? "
            "ORDER BY active DESC, id LIMIT 1",
            (name, name),
        ).fetchone()
        if row is not None:
            return row

    ip_candidates = []
    for value in (
        getattr(data, "route_ip", ""),
        hit["route_ip"] if hit is not None else "",
        getattr(data, "gateway_name", ""),
        hit["gateway_name"] if hit is not None else "",
    ):
        ip = _clean_route_ip(value)
        if ip and ip not in ip_candidates:
            ip_candidates.append(ip)
    if not ip_candidates:
        return None

    split_ip_list = getattr(db, "split_ip_list", lambda value: [x.strip() for x in re.split(r"[\s,;]+", value or "") if x.strip()])
    for group in conn.execute("SELECT id, name, ips FROM termination_groups WHERE active = 1 ORDER BY id").fetchall():
        for token in split_ip_list(group["ips"]):
            token = _clean_route_ip(token)
            for candidate in ip_candidates:
                if db.ip_token_matches(candidate, token):
                    return group
    return None


def _deduct_termination_group_balance(conn, db, data, cost_units):
    cost_units = int(cost_units or 0)
    if cost_units <= 0:
        return None, "", 0
    group = _resolve_termination_group(conn, db, data)
    if group is None:
        print(
            f"[SUPPLIER BALANCE WARN] group not found for call={getattr(data, 'call_uuid', '')} "
            f"terminator_id={getattr(data, 'terminator_id', None)} "
            f"terminator={getattr(data, 'terminator_name', '')} "
            f"gateway={getattr(data, 'gateway_name', '')} route_ip={getattr(data, 'route_ip', '')} "
            f"cost={cost_units}"
        )
        return None, "", 0
    conn.execute("UPDATE termination_groups SET balance_cents = balance_cents - ? WHERE id = ?", (cost_units, group["id"]))
    return group["id"], group["name"], cost_units


def install_call_routes(app, main, db):
    @app.post("/api/reserve", dependencies=main.API_AUTH)
    def reserve(data: main.ReserveIn):
        ensure_schema(db)
        conn = db.get_conn()
        client = rate = route = None
        stage = "received"
        client_tech_prefix = dial_destination = gateway_name = route_ip = provider_number = ""
        max_seconds = None
        try:
            now_ts = db.now()
            conn.execute("BEGIN IMMEDIATE")
            db.cleanup_expired(conn, now_ts)
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
            rate_match = db.match_client_rate_for_destination(conn, client["id"], data.destination, route_seed)
            if rate_match is None:
                raise HTTPException(403, f"Нет тарифа клиента для {data.destination}")
            rate, dial_destination, client_tech_prefix = rate_match
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
            stage = "balance"
            active_calls = active_call_count(conn, client["id"], now_ts)
            spendable_balance = available_client_units(client, 0)
            if spendable_balance <= 0:
                raise HTTPException(403, "Недостаточно средств")
            per_call_balance = math.floor(spendable_balance / (active_calls + 1))
            max_seconds = math.floor(per_call_balance / rate["sell_rate_cents"] * 60)
            if max_seconds <= 0:
                raise HTTPException(403, "Недостаточно средств даже на 1 секунду разговора")

            call_uuid = data.call_uuid or f"nouuid-{now_ts}-{client['id']}"
            reserved = math.ceil(max_seconds * rate["sell_rate_cents"] / 60)
            expires_at = now_ts + max_seconds + db.RESERVATION_BUFFER_SEC
            conn.execute(
                "INSERT OR REPLACE INTO reservations (client_id, call_uuid, reserved_cents, expires_at) VALUES (?, ?, ?, ?)",
                (client["id"], call_uuid, reserved, expires_at),
            )
            conn.commit()
            stage = "reserved"
            reason = f"Пропущен; активных звонков={active_calls}; лимит={max_seconds} сек"
            main._safe_record_sip_hit(
                data,
                status_text="allowed",
                stage=stage,
                reason=reason,
                client=client,
                rate=rate,
                route=route,
                gateway_name=gateway_name,
                route_ip=route_ip,
                dial_destination=dial_destination,
                provider_number=provider_number,
                client_tech_prefix=client_tech_prefix,
                max_seconds=max_seconds,
            )
            return {
                "allowed": True,
                "max_seconds": max_seconds,
                "sell_rate_cents": rate["sell_rate_cents"],
                "cost_rate_cents": route["cost_rate_cents"],
                "gateway_name": gateway_name,
                "route_ip": route_ip,
                "tech_prefix": route["tech_prefix"],
                "client_tech_prefix": client_tech_prefix,
                "dial_destination": dial_destination,
                "provider_number": provider_number,
                "terminator_id": route["id"],
                "terminator_name": route["name"],
                "terminator_destination_name": route["destination_name"],
                "terminator_prefix": route["prefix"],
                "terminator_tech_prefix": route["tech_prefix"],
                "client_id": client["id"],
                "call_uuid": call_uuid,
            }
        except HTTPException as exc:
            conn.rollback()
            main._safe_record_sip_hit(
                data,
                status_text="rejected",
                stage=stage,
                reason=str(exc.detail),
                client=client,
                rate=rate,
                route=route,
                gateway_name=gateway_name,
                route_ip=route_ip,
                dial_destination=dial_destination,
                provider_number=provider_number,
                client_tech_prefix=client_tech_prefix,
                max_seconds=max_seconds,
            )
            raise
        finally:
            conn.close()

    @app.post("/api/finalize", dependencies=main.API_AUTH)
    def finalize(data: main.FinalizeIn):
        ensure_schema(db)
        conn = db.get_conn()
        try:
            sell_billing_cycle = db.normalize_billing_cycle(data.sell_billing_cycle)
            cost_billing_cycle = db.normalize_billing_cycle(data.cost_billing_cycle)
            bsec = db.billed_seconds(data.billsec, sell_billing_cycle)
            charged = db.charge_units(data.sell_rate_cents, data.billsec, sell_billing_cycle)
            cost = db.charge_units(data.cost_rate_cents, data.billsec, cost_billing_cycle)
            conn.execute("BEGIN IMMEDIATE")
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (data.client_id,)).fetchone()
            if client is None:
                raise HTTPException(404, "Клиент не найден")

            new_balance = client["balance_cents"] - charged
            min_balance = minimum_client_balance_units(client)
            if new_balance < min_balance:
                max_charge = max_charge_units_for_client(client)
                print(
                    f"[FINALIZE WARN] client={data.client_id} call={data.call_uuid} "
                    f"charged={charged} exceeds available credit={max_charge}: clamp to limit {min_balance}"
                )
                charged = min(charged, max_charge)
                new_balance = client["balance_cents"] - charged

            margin = charged - cost
            conn.execute("UPDATE clients SET balance_cents = ? WHERE id = ?", (new_balance, data.client_id))
            termination_group_id, termination_group_name, supplier_debit = _deduct_termination_group_balance(conn, db, data, cost)
            conn.execute(
                "INSERT INTO cdr (client_id, call_uuid, sip_ip, clid, destination, client_tech_prefix, "
                "dial_destination, provider_number, gateway_name, route_ip, terminator_id, terminator_name, "
                "terminator_destination_name, terminator_prefix, terminator_tech_prefix, hangup_cause, "
                "bridge_hangup_cause, result, billsec, sell_rate_cents, cost_rate_cents, sell_billing_cycle, "
                "cost_billing_cycle, charged_cents, margin_cents, termination_group_id, termination_group_name, "
                "supplier_balance_debit_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data.client_id,
                    data.call_uuid,
                    data.sip_ip,
                    data.clid,
                    data.destination,
                    data.client_tech_prefix,
                    data.dial_destination,
                    data.provider_number,
                    data.gateway_name,
                    data.route_ip,
                    data.terminator_id,
                    data.terminator_name,
                    data.terminator_destination_name,
                    data.terminator_prefix,
                    data.terminator_tech_prefix,
                    data.hangup_cause,
                    data.bridge_hangup_cause,
                    data.result,
                    data.billsec,
                    data.sell_rate_cents,
                    data.cost_rate_cents,
                    sell_billing_cycle,
                    cost_billing_cycle,
                    charged,
                    margin,
                    termination_group_id,
                    termination_group_name,
                    supplier_debit,
                ),
            )

            final_status = "answered" if data.billsec > 0 else "failed"
            hangup = data.bridge_hangup_cause or data.hangup_cause or data.result or ""
            reason_parts = [part for part in (hangup, data.result if data.result != hangup else "") if part]
            reason_parts.append(f"billsec={data.billsec}")
            if charged:
                reason_parts.append(f"charged={charged}")
            if margin:
                reason_parts.append(f"margin={margin}")
            conn.execute(
                "UPDATE sip_hits SET status = ?, stage = ?, reason = ?, client_id = ?, client_name = ?, "
                "client_tech_prefix = ?, dial_destination = ?, provider_number = ?, gateway_name = ?, "
                "route_ip = ?, terminator_id = ?, terminator_name = ?, terminator_destination_name = ?, "
                "terminator_prefix = ?, sell_rate_cents = ?, cost_rate_cents = ?, sell_billing_cycle = ?, "
                "cost_billing_cycle = ? WHERE call_uuid = ?",
                (
                    final_status,
                    "finalized",
                    " · ".join(reason_parts),
                    data.client_id,
                    client["name"],
                    data.client_tech_prefix,
                    data.dial_destination,
                    data.provider_number,
                    data.gateway_name or "",
                    data.route_ip,
                    data.terminator_id,
                    data.terminator_name,
                    data.terminator_destination_name,
                    data.terminator_prefix,
                    data.sell_rate_cents,
                    data.cost_rate_cents,
                    sell_billing_cycle,
                    cost_billing_cycle,
                    data.call_uuid,
                ),
            )

            conn.execute("DELETE FROM reservations WHERE call_uuid = ?", (data.call_uuid,))
            conn.commit()
            return {
                "ok": True,
                "charged_cents": charged,
                "margin_cents": margin,
                "balance_cents": new_balance,
                "billsec": data.billsec,
                "billed_seconds": bsec,
                "sell_billing_cycle": sell_billing_cycle,
                "cost_billing_cycle": cost_billing_cycle,
                "termination_group_id": termination_group_id,
                "termination_group_name": termination_group_name,
                "supplier_balance_debit_cents": supplier_debit,
            }
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/api/ops/client-balance-adjust", dependencies=main.API_AUTH)
    def ops_client_balance_adjust(data: OpsBalanceAdjustIn):
        if data.amount_cents == 0:
            raise HTTPException(400, "amount_cents must be non-zero")
        ensure_schema(db)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (data.client_id,)).fetchone()
            if client is None:
                conn.rollback()
                raise HTTPException(404, "Клиент не найден")
            new_balance = int(client["balance_cents"]) + int(data.amount_cents)
            if new_balance < minimum_client_balance_units(client):
                conn.rollback()
                raise HTTPException(409, "Корректировка уведет баланс ниже кредитного лимита")
            conn.execute("UPDATE clients SET balance_cents = ? WHERE id = ?", (new_balance, data.client_id))
            conn.commit()
            return {
                "ok": True,
                "client_id": data.client_id,
                "old_balance_cents": int(client["balance_cents"]),
                "adjustment_cents": int(data.amount_cents),
                "balance_cents": new_balance,
                "credit_limit_cents": client_credit_limit_units(client),
            }
        finally:
            conn.close()
