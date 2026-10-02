"""Isolated inbound DID routing and prepaid billing module."""

import ipaddress
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _clean_text(value: str, limit: int = 255) -> str:
    return (value or "").strip()[:limit]


def _source_allowed(source_ip: str, raw_whitelist: str) -> bool:
    tokens = [x.strip() for x in re.split(r"[\s,;]+", raw_whitelist or "") if x.strip()]
    if not tokens:
        return True
    try:
        candidate = ipaddress.ip_address((source_ip or "").strip())
    except ValueError:
        return False
    for token in tokens:
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        if candidate in network:
            return True
    return False


def _bridge_target(destination: str, did_number: str) -> str:
    destination = (destination or "").strip()
    if destination.startswith("sofia/"):
        return destination.replace("{did}", did_number)
    if destination.startswith("sip:"):
        destination = destination[4:]
    if "@" in destination:
        user, host = destination.split("@", 1)
        return f"sofia/external/{user or did_number}@{host}"
    return f"sofia/external/{did_number}@{destination}"


class DidClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    balance_cents: int = Field(default=0, ge=0)
    credit_limit_cents: int = Field(default=0, ge=0)
    outbound_ips: str = Field(default="", max_length=2000)
    outbound_tech_prefix: str = Field(default="", max_length=32)
    active: bool = True


class DidClientUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=8)
    credit_limit_cents: Optional[int] = Field(default=None, ge=0)
    outbound_ips: Optional[str] = Field(default=None, max_length=2000)
    outbound_tech_prefix: Optional[str] = Field(default=None, max_length=32)
    active: Optional[bool] = None


class DidTopupIn(BaseModel):
    amount_cents: int


class DidNumberIn(BaseModel):
    client_id: int
    did_number: str = Field(min_length=3, max_length=32)
    provider_name: str = Field(default="", max_length=120)
    provider_ips: str = Field(default="", max_length=2000)
    destination: str = Field(min_length=3, max_length=500)
    backup_destination: str = Field(default="", max_length=500)
    sell_rate_cents: int = Field(ge=0)
    cost_rate_cents: int = Field(default=0, ge=0)
    billing_cycle: str = "1/1"
    max_channels: int = Field(default=1, ge=1, le=1000)
    active: bool = True
    notes: str = Field(default="", max_length=2000)


class DidNumberUpdate(BaseModel):
    client_id: Optional[int] = None
    did_number: Optional[str] = Field(default=None, min_length=3, max_length=32)
    provider_name: Optional[str] = Field(default=None, max_length=120)
    provider_ips: Optional[str] = Field(default=None, max_length=2000)
    destination: Optional[str] = Field(default=None, min_length=3, max_length=500)
    backup_destination: Optional[str] = Field(default=None, max_length=500)
    sell_rate_cents: Optional[int] = Field(default=None, ge=0)
    cost_rate_cents: Optional[int] = Field(default=None, ge=0)
    billing_cycle: Optional[str] = None
    max_channels: Optional[int] = Field(default=None, ge=1, le=1000)
    active: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


class DidReserveIn(BaseModel):
    did_number: str
    caller_id: str = ""
    source_ip: str = ""
    call_uuid: str = Field(min_length=1, max_length=255)


class DidFinalizeIn(BaseModel):
    call_uuid: str = Field(min_length=1, max_length=255)
    billsec: int = Field(ge=0)
    hangup_cause: str = ""
    result: str = ""


class DidCallerIdsIn(BaseModel):
    numbers: str = Field(default="", max_length=20000)


class DidOutboundRouteIn(BaseModel):
    client_id: int
    terminator_id: Optional[int] = None
    provider_name: str = Field(default="", max_length=120)
    destination_name: str = Field(default="", max_length=120)
    prefix: str = Field(default="", max_length=32)
    gateway_name: str = Field(default="", max_length=255)
    route_ips: str = Field(default="", max_length=2000)
    tech_prefix: str = Field(default="", max_length=32)
    sell_rate_cents: int = Field(ge=0)
    cost_rate_cents: int = Field(default=0, ge=0)
    billing_cycle: str = "1/1"
    max_channels: int = Field(default=10, ge=1, le=1000)
    active: bool = True


class DidOutboundRouteUpdate(BaseModel):
    client_id: Optional[int] = None
    terminator_id: Optional[int] = None
    provider_name: Optional[str] = Field(default=None, max_length=120)
    destination_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    prefix: Optional[str] = Field(default=None, min_length=1, max_length=32)
    gateway_name: Optional[str] = Field(default=None, max_length=255)
    route_ips: Optional[str] = Field(default=None, max_length=2000)
    tech_prefix: Optional[str] = Field(default=None, max_length=32)
    sell_rate_cents: Optional[int] = Field(default=None, ge=0)
    cost_rate_cents: Optional[int] = Field(default=None, ge=0)
    billing_cycle: Optional[str] = None
    max_channels: Optional[int] = Field(default=None, ge=1, le=1000)
    active: Optional[bool] = None


class DidOutboundReserveIn(BaseModel):
    source_ip: str
    destination: str
    caller_id: str
    call_uuid: str = Field(min_length=1, max_length=255)


class DidOutboundFinalizeIn(BaseModel):
    call_uuid: str = Field(min_length=1, max_length=255)
    billsec: int = Field(ge=0)
    hangup_cause: str = ""
    result: str = ""


def init_schema(db) -> None:
    conn = db.get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS did_clients (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                name               TEXT NOT NULL UNIQUE,
                balance_cents      INTEGER NOT NULL DEFAULT 0,
                credit_limit_cents INTEGER NOT NULL DEFAULT 0,
                currency           TEXT NOT NULL DEFAULT 'USD',
                outbound_ips       TEXT NOT NULL DEFAULT '',
                outbound_tech_prefix TEXT NOT NULL DEFAULT '',
                active             INTEGER NOT NULL DEFAULT 1,
                created_at         TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS did_numbers (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id          INTEGER NOT NULL REFERENCES did_clients(id),
                did_number         TEXT NOT NULL UNIQUE,
                provider_name      TEXT NOT NULL DEFAULT '',
                provider_ips       TEXT NOT NULL DEFAULT '',
                destination        TEXT NOT NULL,
                backup_destination TEXT NOT NULL DEFAULT '',
                sell_rate_cents    INTEGER NOT NULL DEFAULT 0,
                cost_rate_cents    INTEGER NOT NULL DEFAULT 0,
                billing_cycle      TEXT NOT NULL DEFAULT '1/1',
                cost_billing_cycle TEXT NOT NULL DEFAULT '1/1',
                max_channels       INTEGER NOT NULL DEFAULT 1,
                active             INTEGER NOT NULL DEFAULT 1,
                notes              TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_did_numbers_client ON did_numbers(client_id);

            CREATE TABLE IF NOT EXISTS did_caller_ids (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id          INTEGER NOT NULL REFERENCES did_clients(id) ON DELETE CASCADE,
                caller_id          TEXT NOT NULL UNIQUE,
                created_at         TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_did_caller_ids_client ON did_caller_ids(client_id);

            CREATE TABLE IF NOT EXISTS did_reservations (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid          TEXT NOT NULL UNIQUE,
                did_number_id      INTEGER NOT NULL REFERENCES did_numbers(id),
                client_id          INTEGER NOT NULL REFERENCES did_clients(id),
                caller_id          TEXT NOT NULL DEFAULT '',
                source_ip          TEXT NOT NULL DEFAULT '',
                destination        TEXT NOT NULL DEFAULT '',
                reserved_cents     INTEGER NOT NULL DEFAULT 0,
                max_seconds        INTEGER NOT NULL DEFAULT 0,
                active             INTEGER NOT NULL DEFAULT 1,
                expires_at         INTEGER NOT NULL,
                created_at         TEXT NOT NULL DEFAULT (datetime('now')),
                finalized_at       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_did_reservations_number
                ON did_reservations(did_number_id, active, expires_at);

            CREATE TABLE IF NOT EXISTS did_cdr (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid          TEXT NOT NULL UNIQUE,
                did_number_id      INTEGER NOT NULL,
                client_id          INTEGER NOT NULL,
                did_number         TEXT NOT NULL,
                caller_id          TEXT NOT NULL DEFAULT '',
                source_ip          TEXT NOT NULL DEFAULT '',
                destination        TEXT NOT NULL DEFAULT '',
                provider_name      TEXT NOT NULL DEFAULT '',
                billsec            INTEGER NOT NULL DEFAULT 0,
                billed_seconds     INTEGER NOT NULL DEFAULT 0,
                sell_rate_cents    INTEGER NOT NULL DEFAULT 0,
                cost_rate_cents    INTEGER NOT NULL DEFAULT 0,
                billing_cycle      TEXT NOT NULL DEFAULT '1/1',
                charged_cents      INTEGER NOT NULL DEFAULT 0,
                cost_cents         INTEGER NOT NULL DEFAULT 0,
                margin_cents       INTEGER NOT NULL DEFAULT 0,
                hangup_cause       TEXT NOT NULL DEFAULT '',
                result             TEXT NOT NULL DEFAULT '',
                started_at         TEXT NOT NULL,
                ended_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_did_cdr_started ON did_cdr(started_at);

            CREATE TABLE IF NOT EXISTS did_balance_transactions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id          INTEGER NOT NULL REFERENCES did_clients(id),
                amount_cents       INTEGER NOT NULL,
                kind               TEXT NOT NULL,
                reference          TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS did_outbound_routes (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id          INTEGER NOT NULL REFERENCES did_clients(id) ON DELETE CASCADE,
                terminator_id      INTEGER REFERENCES terminators(id),
                provider_name      TEXT NOT NULL DEFAULT '',
                destination_name   TEXT NOT NULL,
                prefix             TEXT NOT NULL,
                gateway_name       TEXT NOT NULL DEFAULT '',
                route_ips          TEXT NOT NULL DEFAULT '',
                tech_prefix        TEXT NOT NULL DEFAULT '',
                sell_rate_cents    INTEGER NOT NULL DEFAULT 0,
                cost_rate_cents    INTEGER NOT NULL DEFAULT 0,
                billing_cycle      TEXT NOT NULL DEFAULT '1/1',
                max_channels       INTEGER NOT NULL DEFAULT 10,
                active             INTEGER NOT NULL DEFAULT 1,
                created_at         TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(client_id, prefix, provider_name)
            );
            CREATE INDEX IF NOT EXISTS idx_did_out_routes_client
                ON did_outbound_routes(client_id, active, prefix);

            CREATE TABLE IF NOT EXISTS did_outbound_reservations (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid          TEXT NOT NULL UNIQUE,
                route_id           INTEGER NOT NULL REFERENCES did_outbound_routes(id),
                client_id          INTEGER NOT NULL REFERENCES did_clients(id),
                source_ip          TEXT NOT NULL DEFAULT '',
                caller_id          TEXT NOT NULL,
                destination        TEXT NOT NULL,
                dial_destination   TEXT NOT NULL,
                provider_number    TEXT NOT NULL,
                bridge_target      TEXT NOT NULL,
                reserved_cents     INTEGER NOT NULL DEFAULT 0,
                max_seconds        INTEGER NOT NULL DEFAULT 0,
                active             INTEGER NOT NULL DEFAULT 1,
                expires_at         INTEGER NOT NULL,
                created_at         TEXT NOT NULL DEFAULT (datetime('now')),
                finalized_at       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_did_out_reservations_route
                ON did_outbound_reservations(route_id, active, expires_at);

            CREATE TABLE IF NOT EXISTS did_outbound_cdr (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid          TEXT NOT NULL UNIQUE,
                route_id           INTEGER NOT NULL,
                client_id          INTEGER NOT NULL,
                source_ip          TEXT NOT NULL DEFAULT '',
                caller_id          TEXT NOT NULL,
                destination        TEXT NOT NULL,
                dial_destination   TEXT NOT NULL,
                provider_number    TEXT NOT NULL,
                provider_name      TEXT NOT NULL DEFAULT '',
                destination_name   TEXT NOT NULL DEFAULT '',
                billsec            INTEGER NOT NULL DEFAULT 0,
                billed_seconds     INTEGER NOT NULL DEFAULT 0,
                sell_rate_cents    INTEGER NOT NULL DEFAULT 0,
                cost_rate_cents    INTEGER NOT NULL DEFAULT 0,
                billing_cycle      TEXT NOT NULL DEFAULT '1/1',
                charged_cents      INTEGER NOT NULL DEFAULT 0,
                cost_cents         INTEGER NOT NULL DEFAULT 0,
                margin_cents       INTEGER NOT NULL DEFAULT 0,
                hangup_cause       TEXT NOT NULL DEFAULT '',
                result             TEXT NOT NULL DEFAULT '',
                started_at         TEXT NOT NULL,
                ended_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_did_out_cdr_started ON did_outbound_cdr(started_at);
            """
        )
        client_columns = {row["name"] for row in conn.execute("PRAGMA table_info(did_clients)")}
        if "outbound_ips" not in client_columns:
            conn.execute("ALTER TABLE did_clients ADD COLUMN outbound_ips TEXT NOT NULL DEFAULT ''")
        if "outbound_tech_prefix" not in client_columns:
            conn.execute("ALTER TABLE did_clients ADD COLUMN outbound_tech_prefix TEXT NOT NULL DEFAULT ''")
        route_columns = {row["name"] for row in conn.execute("PRAGMA table_info(did_outbound_routes)")}
        if "terminator_id" not in route_columns:
            conn.execute("ALTER TABLE did_outbound_routes ADD COLUMN terminator_id INTEGER REFERENCES terminators(id)")
        if "cost_billing_cycle" not in route_columns:
            conn.execute("ALTER TABLE did_outbound_routes ADD COLUMN cost_billing_cycle TEXT NOT NULL DEFAULT '1/1'")
        conn.commit()
    finally:
        conn.close()


def reserve_call(db, data: DidReserveIn):
    did_number = _digits(data.did_number)
    if not did_number:
        raise HTTPException(422, "Некорректный DID номер")
    now = int(time.time())
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT r.*, n.did_number, n.provider_name, n.provider_ips,
                      n.backup_destination, n.sell_rate_cents, n.cost_rate_cents,
                      n.billing_cycle, c.currency
                 FROM did_reservations r
                 JOIN did_numbers n ON n.id = r.did_number_id
                 JOIN did_clients c ON c.id = r.client_id
                WHERE r.call_uuid = ?""",
            (data.call_uuid,),
        ).fetchone()
        if existing is not None and existing["active"] and existing["expires_at"] > now:
            conn.commit()
            return _reserve_response(existing)
        if existing is not None:
            raise HTTPException(409, "Этот Call UUID уже завершён или истёк")

        conn.execute(
            "UPDATE did_reservations SET active = 0 WHERE active = 1 AND expires_at <= ?",
            (now,),
        )
        route = conn.execute(
            """SELECT n.*, c.name AS client_name, c.balance_cents,
                      c.credit_limit_cents, c.currency, c.active AS client_active
                 FROM did_numbers n
                 JOIN did_clients c ON c.id = n.client_id
                WHERE n.did_number = ?""",
            (did_number,),
        ).fetchone()
        if route is None:
            raise HTTPException(404, "DID номер не найден")
        if not route["active"] or not route["client_active"]:
            raise HTTPException(403, "DID или клиент выключен")
        if not _source_allowed(data.source_ip, route["provider_ips"]):
            raise HTTPException(403, "IP поставщика не входит в whitelist DID")

        active_count = conn.execute(
            "SELECT COUNT(*) FROM did_reservations WHERE did_number_id = ? AND active = 1 AND expires_at > ?",
            (route["id"], now),
        ).fetchone()[0]
        if active_count >= route["max_channels"]:
            raise HTTPException(429, "Лимит одновременных входящих линий исчерпан")

        held = conn.execute(
            """SELECT
                 COALESCE((SELECT SUM(reserved_cents) FROM did_reservations
                            WHERE client_id = ? AND active = 1 AND expires_at > ?), 0) +
                 COALESCE((SELECT SUM(reserved_cents) FROM did_outbound_reservations
                            WHERE client_id = ? AND active = 1 AND expires_at > ?), 0)""",
            (route["client_id"], now, route["client_id"], now),
        ).fetchone()[0]
        available = int(route["balance_cents"]) + int(route["credit_limit_cents"]) - int(held)
        rate = int(route["sell_rate_cents"])
        if rate > 0 and available <= 0:
            raise HTTPException(402, "Недостаточно средств на DID-балансе")

        open_slots = max(1, int(route["max_channels"]) - int(active_count))
        allocated = max(0, available // open_slots)
        if rate > 0:
            max_seconds = db.max_seconds_for_balance(allocated, rate, route["billing_cycle"])
            if max_seconds <= 0:
                raise HTTPException(402, "Недостаточно средств даже на первый интервал")
            reserved = db.charge_units(rate, max_seconds, route["billing_cycle"])
        else:
            max_seconds = 86400
            reserved = 0
        expires_at = now + max_seconds + db.RESERVATION_BUFFER_SEC
        destination = route["destination"]
        conn.execute(
            """INSERT INTO did_reservations
                   (call_uuid, did_number_id, client_id, caller_id, source_ip,
                    destination, reserved_cents, max_seconds, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.call_uuid,
                route["id"],
                route["client_id"],
                _clean_text(data.caller_id, 64),
                _clean_text(data.source_ip, 64),
                destination,
                reserved,
                max_seconds,
                expires_at,
            ),
        )
        conn.commit()
        result = dict(route)
        result.update(
            call_uuid=data.call_uuid,
            caller_id=data.caller_id,
            source_ip=data.source_ip,
            destination=destination,
            reserved_cents=reserved,
            max_seconds=max_seconds,
            expires_at=expires_at,
        )
        return _reserve_response(result)
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(409, "Call UUID уже используется") from exc
    finally:
        conn.close()


def _reserve_response(row):
    did_number = row["did_number"]
    return {
        "allowed": True,
        "call_uuid": row["call_uuid"],
        "client_id": row["client_id"],
        "did_number_id": row["did_number_id"] if "did_number_id" in row.keys() else row["id"],
        "did_number": did_number,
        "destination": row["destination"],
        "backup_destination": row["backup_destination"],
        "bridge_target": _bridge_target(row["destination"], did_number),
        "backup_bridge_target": _bridge_target(row["backup_destination"], did_number) if row["backup_destination"] else "",
        "max_seconds": row["max_seconds"],
        "sell_rate_cents": row["sell_rate_cents"],
        "cost_rate_cents": row["cost_rate_cents"],
        "billing_cycle": row["billing_cycle"],
        "provider_name": row["provider_name"],
        "currency": row["currency"],
    }


def finalize_call(db, data: DidFinalizeIn):
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM did_cdr WHERE call_uuid = ?", (data.call_uuid,)).fetchone()
        if existing is not None:
            conn.commit()
            result = dict(existing)
            result["ok"] = True
            result["idempotent"] = True
            result["balance_cents"] = conn.execute(
                "SELECT balance_cents FROM did_clients WHERE id = ?", (existing["client_id"],)
            ).fetchone()[0]
            return result

        row = conn.execute(
            """SELECT r.*, n.did_number, n.provider_name, n.sell_rate_cents,
                      n.cost_rate_cents, n.billing_cycle, c.balance_cents,
                      c.credit_limit_cents
                 FROM did_reservations r
                 JOIN did_numbers n ON n.id = r.did_number_id
                 JOIN did_clients c ON c.id = r.client_id
                WHERE r.call_uuid = ?""",
            (data.call_uuid,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Резерв входящего звонка не найден")

        billed = db.billed_seconds(data.billsec, row["billing_cycle"])
        charged = db.charge_units(row["sell_rate_cents"], data.billsec, row["billing_cycle"])
        cost = db.charge_units(row["cost_rate_cents"], data.billsec, row["billing_cycle"])
        minimum = -int(row["credit_limit_cents"])
        charged = min(charged, max(0, int(row["balance_cents"]) - minimum))
        new_balance = int(row["balance_cents"]) - charged
        margin = charged - cost
        conn.execute("UPDATE did_clients SET balance_cents = ? WHERE id = ?", (new_balance, row["client_id"]))
        conn.execute(
            """INSERT INTO did_cdr
                   (call_uuid, did_number_id, client_id, did_number, caller_id,
                    source_ip, destination, provider_name, billsec, billed_seconds,
                    sell_rate_cents, cost_rate_cents, billing_cycle, charged_cents,
                    cost_cents, margin_cents, hangup_cause, result, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.call_uuid,
                row["did_number_id"],
                row["client_id"],
                row["did_number"],
                row["caller_id"],
                row["source_ip"],
                row["destination"],
                row["provider_name"],
                data.billsec,
                billed,
                row["sell_rate_cents"],
                row["cost_rate_cents"],
                row["billing_cycle"],
                charged,
                cost,
                margin,
                _clean_text(data.hangup_cause, 120),
                _clean_text(data.result, 120),
                row["created_at"],
            ),
        )
        if charged:
            conn.execute(
                "INSERT INTO did_balance_transactions (client_id, amount_cents, kind, reference) VALUES (?, ?, 'call', ?)",
                (row["client_id"], -charged, data.call_uuid),
            )
        conn.execute(
            "UPDATE did_reservations SET active = 0, finalized_at = datetime('now') WHERE call_uuid = ?",
            (data.call_uuid,),
        )
        conn.commit()
        return {
            "ok": True,
            "idempotent": False,
            "charged_cents": charged,
            "cost_cents": cost,
            "margin_cents": margin,
            "balance_cents": new_balance,
            "billsec": data.billsec,
            "billed_seconds": billed,
        }
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _match_outbound_client(conn, source_ip: str, destination: str):
    candidates = []
    for client in conn.execute(
        "SELECT * FROM did_clients WHERE active = 1 AND trim(outbound_ips) <> '' ORDER BY id"
    ).fetchall():
        if not _source_allowed(source_ip, client["outbound_ips"]):
            continue
        tech_prefix = _digits(client["outbound_tech_prefix"])
        if tech_prefix and not destination.startswith(tech_prefix):
            continue
        dial_destination = destination[len(tech_prefix):] if tech_prefix else destination
        candidates.append((len(tech_prefix), client, dial_destination))
    if not candidates:
        raise HTTPException(403, "IP или техпрефикс не принадлежат DID-клиенту")
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score = candidates[0][0]
    best = [item for item in candidates if item[0] == best_score]
    if len(best) > 1:
        raise HTTPException(409, "IP клиента неоднозначен: назначьте разные техпрефиксы")
    return best[0][1], best[0][2]


def _caller_id_owned_by_client(conn, client_id: int, caller_id: str) -> bool:
    return conn.execute(
        """SELECT 1
             FROM (
                   SELECT did_number AS caller_id FROM did_numbers
                    WHERE client_id = ? AND active = 1
                   UNION ALL
                   SELECT caller_id FROM did_caller_ids WHERE client_id = ?
                  ) owned
            WHERE caller_id = ? LIMIT 1""",
        (client_id, client_id, caller_id),
    ).fetchone() is not None


def _outbound_reserve_response(row):
    return {
        "allowed": True,
        "call_uuid": row["call_uuid"],
        "client_id": row["client_id"],
        "route_id": row["route_id"] if "route_id" in row.keys() else row["id"],
        "caller_id": row["caller_id"],
        "destination": row["destination"],
        "dial_destination": row["dial_destination"],
        "provider_number": row["provider_number"],
        "bridge_target": row["bridge_target"],
        "max_seconds": row["max_seconds"],
        "sell_rate_cents": row["sell_rate_cents"],
        "cost_rate_cents": row["cost_rate_cents"],
        "billing_cycle": row["billing_cycle"],
        "cost_billing_cycle": row["cost_billing_cycle"] if "cost_billing_cycle" in row.keys() else row["billing_cycle"],
        "provider_name": row["provider_name"],
        "destination_name": row["destination_name"],
        "currency": row["currency"],
    }


def _resolve_outbound_route(db, conn, route):
    resolved = dict(route)
    terminator_id = resolved.get("terminator_id")
    if not terminator_id:
        resolved.setdefault("cost_billing_cycle", resolved.get("billing_cycle") or db.DEFAULT_BILLING_CYCLE)
        return resolved
    terminator = db.get_terminator(conn, terminator_id)
    if terminator is None or not terminator["active"]:
        return None
    group = db.get_termination_group(conn, terminator["gateway_group_id"])
    if group is not None and not group["active"]:
        return None
    resolved.update(
        provider_name=((group["name"] if group else "") or terminator["name"]),
        destination_name=terminator["destination_name"],
        prefix=_digits(terminator["prefix"]),
        gateway_name=((terminator["gateway_name"] or "") or ((group["gateway_name"] or "") if group else "")),
        route_ips=((terminator["ips"] or "") or ((group["ips"] or "") if group else "")),
        tech_prefix=terminator["tech_prefix"] or "",
        cost_rate_cents=terminator["cost_rate_cents"],
        cost_billing_cycle=db.normalize_billing_cycle(terminator["billing_cycle"]),
    )
    return resolved


def reserve_outbound_call(db, data: DidOutboundReserveIn):
    destination = _digits(data.destination)
    caller_id = _digits(data.caller_id)
    if not destination:
        raise HTTPException(422, "Некорректный B-номер")
    if not caller_id:
        raise HTTPException(403, "Caller ID обязателен для DID-исходящего маршрута")
    now = int(time.time())
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT r.*, o.provider_name, o.destination_name, o.sell_rate_cents,
                      o.cost_rate_cents, o.billing_cycle, c.currency
                 FROM did_outbound_reservations r
                 JOIN did_outbound_routes o ON o.id = r.route_id
                 JOIN did_clients c ON c.id = r.client_id
                WHERE r.call_uuid = ?""",
            (data.call_uuid,),
        ).fetchone()
        if existing is not None and existing["active"] and existing["expires_at"] > now:
            conn.commit()
            return _outbound_reserve_response(existing)
        if existing is not None:
            raise HTTPException(409, "Этот Call UUID уже завершён или истёк")

        conn.execute("UPDATE did_reservations SET active = 0 WHERE active = 1 AND expires_at <= ?", (now,))
        conn.execute("UPDATE did_outbound_reservations SET active = 0 WHERE active = 1 AND expires_at <= ?", (now,))
        client, dial_destination = _match_outbound_client(conn, data.source_ip, destination)
        if not dial_destination:
            raise HTTPException(422, "После техпрефикса отсутствует B-номер")
        if not _caller_id_owned_by_client(conn, client["id"], caller_id):
            raise HTTPException(403, "Caller ID не входит в whitelist этого DID-клиента")

        route_rows = conn.execute(
            "SELECT * FROM did_outbound_routes WHERE client_id = ? AND active = 1 ORDER BY length(prefix) DESC, id",
            (client["id"],),
        ).fetchall()
        routes = [resolved for item in route_rows if (resolved := _resolve_outbound_route(db, conn, item)) is not None]
        routes.sort(key=lambda item: len(_digits(item["prefix"])), reverse=True)
        route = next((item for item in routes if dial_destination.startswith(_digits(item["prefix"]))), None)
        if route is None:
            raise HTTPException(404, "Для B-номера нет исходящего DID-маршрута")
        route_ip = db.pick_ip(route["route_ips"], data.call_uuid)
        gateway = (route["gateway_name"] or "").strip()
        if not gateway and not route_ip:
            raise HTTPException(503, "У исходящего маршрута нет gateway или IP поставщика")

        active_count = conn.execute(
            "SELECT COUNT(*) FROM did_outbound_reservations WHERE route_id = ? AND active = 1 AND expires_at > ?",
            (route["id"], now),
        ).fetchone()[0]
        if active_count >= route["max_channels"]:
            raise HTTPException(429, "Лимит исходящих линий маршрута исчерпан")
        held = conn.execute(
            """SELECT
                 COALESCE((SELECT SUM(reserved_cents) FROM did_reservations
                            WHERE client_id = ? AND active = 1 AND expires_at > ?), 0) +
                 COALESCE((SELECT SUM(reserved_cents) FROM did_outbound_reservations
                            WHERE client_id = ? AND active = 1 AND expires_at > ?), 0)""",
            (client["id"], now, client["id"], now),
        ).fetchone()[0]
        available = int(client["balance_cents"]) + int(client["credit_limit_cents"]) - int(held)
        rate = int(route["sell_rate_cents"])
        if rate > 0 and available <= 0:
            raise HTTPException(402, "Недостаточно средств на DID-балансе")
        open_slots = max(1, int(route["max_channels"]) - int(active_count))
        allocated = max(0, available // open_slots)
        if rate > 0:
            max_seconds = db.max_seconds_for_balance(allocated, rate, route["billing_cycle"])
            if max_seconds <= 0:
                raise HTTPException(402, "Недостаточно средств даже на первый интервал")
            reserved = db.charge_units(rate, max_seconds, route["billing_cycle"])
        else:
            max_seconds = 86400
            reserved = 0

        provider_number = f"{_digits(route['tech_prefix'])}{dial_destination}"
        bridge_target = (
            f"sofia/gateway/{gateway}/{provider_number}"
            if gateway else f"sofia/external/{provider_number}@{route_ip}"
        )
        expires_at = now + max_seconds + db.RESERVATION_BUFFER_SEC
        conn.execute(
            """UPDATE did_outbound_routes
                  SET provider_name = ?, destination_name = ?, prefix = ?, gateway_name = ?,
                      route_ips = ?, tech_prefix = ?, cost_rate_cents = ?, cost_billing_cycle = ?
                WHERE id = ?""",
            (
                route["provider_name"], route["destination_name"], route["prefix"],
                route["gateway_name"], route["route_ips"], route["tech_prefix"],
                route["cost_rate_cents"], route["cost_billing_cycle"], route["id"],
            ),
        )
        conn.execute(
            """INSERT INTO did_outbound_reservations
                   (call_uuid, route_id, client_id, source_ip, caller_id, destination,
                    dial_destination, provider_number, bridge_target, reserved_cents,
                    max_seconds, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.call_uuid, route["id"], client["id"], _clean_text(data.source_ip, 64),
                caller_id, destination, dial_destination, provider_number, bridge_target,
                reserved, max_seconds, expires_at,
            ),
        )
        conn.commit()
        result = dict(route)
        result.update(
            call_uuid=data.call_uuid, route_id=route["id"], client_id=client["id"],
            caller_id=caller_id, destination=destination, dial_destination=dial_destination,
            provider_number=provider_number, bridge_target=bridge_target,
            reserved_cents=reserved, max_seconds=max_seconds, expires_at=expires_at,
            currency=client["currency"],
        )
        return _outbound_reserve_response(result)
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(409, "Call UUID уже используется") from exc
    finally:
        conn.close()


def finalize_outbound_call(db, data: DidOutboundFinalizeIn):
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM did_outbound_cdr WHERE call_uuid = ?", (data.call_uuid,)).fetchone()
        if existing is not None:
            balance = conn.execute("SELECT balance_cents FROM did_clients WHERE id = ?", (existing["client_id"],)).fetchone()[0]
            conn.commit()
            result = dict(existing)
            result.update(ok=True, idempotent=True, balance_cents=balance)
            return result
        row = conn.execute(
            """SELECT r.*, o.provider_name, o.destination_name, o.sell_rate_cents,
                      o.cost_rate_cents, o.billing_cycle, o.cost_billing_cycle, c.balance_cents,
                      c.credit_limit_cents
                 FROM did_outbound_reservations r
                 JOIN did_outbound_routes o ON o.id = r.route_id
                 JOIN did_clients c ON c.id = r.client_id
                WHERE r.call_uuid = ?""",
            (data.call_uuid,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Резерв исходящего DID-звонка не найден")
        billed = db.billed_seconds(data.billsec, row["billing_cycle"])
        charged = db.charge_units(row["sell_rate_cents"], data.billsec, row["billing_cycle"])
        cost = db.charge_units(row["cost_rate_cents"], data.billsec, row["cost_billing_cycle"])
        minimum = -int(row["credit_limit_cents"])
        charged = min(charged, max(0, int(row["balance_cents"]) - minimum))
        balance = int(row["balance_cents"]) - charged
        margin = charged - cost
        conn.execute("UPDATE did_clients SET balance_cents = ? WHERE id = ?", (balance, row["client_id"]))
        conn.execute(
            """INSERT INTO did_outbound_cdr
                   (call_uuid, route_id, client_id, source_ip, caller_id, destination,
                    dial_destination, provider_number, provider_name, destination_name,
                    billsec, billed_seconds, sell_rate_cents, cost_rate_cents,
                    billing_cycle, charged_cents, cost_cents, margin_cents,
                    hangup_cause, result, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.call_uuid, row["route_id"], row["client_id"], row["source_ip"],
                row["caller_id"], row["destination"], row["dial_destination"],
                row["provider_number"], row["provider_name"], row["destination_name"],
                data.billsec, billed, row["sell_rate_cents"], row["cost_rate_cents"],
                row["billing_cycle"], charged, cost, margin,
                _clean_text(data.hangup_cause, 120), _clean_text(data.result, 120), row["created_at"],
            ),
        )
        if charged:
            conn.execute(
                "INSERT INTO did_balance_transactions (client_id, amount_cents, kind, reference) VALUES (?, ?, 'outbound_call', ?)",
                (row["client_id"], -charged, data.call_uuid),
            )
        conn.execute(
            "UPDATE did_outbound_reservations SET active = 0, finalized_at = datetime('now') WHERE call_uuid = ?",
            (data.call_uuid,),
        )
        conn.commit()
        return {
            "ok": True, "idempotent": False, "charged_cents": charged,
            "cost_cents": cost, "margin_cents": margin, "balance_cents": balance,
            "billsec": data.billsec, "billed_seconds": billed,
        }
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


def install(app, main, db, base_path: Path) -> None:
    init_schema(db)

    @app.on_event("startup")
    def _did_startup():
        init_schema(db)

    @app.get("/dids", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def did_page(request: Request):
        html = (base_path / "dids.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/dids/dashboard", dependencies=main.ADMIN_AUTH)
    def did_dashboard():
        now = int(time.time())
        conn = db.get_conn()
        try:
            conn.execute("UPDATE did_reservations SET active = 0 WHERE active = 1 AND expires_at <= ?", (now,))
            clients = [dict(r) for r in conn.execute(
                """SELECT c.*,
                          COUNT(DISTINCT n.id) AS did_count,
                          (SELECT COUNT(*) FROM did_caller_ids ci WHERE ci.client_id = c.id) AS extra_caller_id_count,
                          COALESCE(SUM(CASE WHEN r.active = 1 AND r.expires_at > ? THEN 1 ELSE 0 END), 0) AS active_calls
                     FROM did_clients c
                     LEFT JOIN did_numbers n ON n.client_id = c.id
                     LEFT JOIN did_reservations r ON r.did_number_id = n.id
                    GROUP BY c.id ORDER BY c.name""",
                (now,),
            ).fetchall()]
            numbers = [dict(r) for r in conn.execute(
                """SELECT n.*, c.name AS client_name, c.currency,
                          COALESCE(SUM(CASE WHEN r.active = 1 AND r.expires_at > ? THEN 1 ELSE 0 END), 0) AS active_calls
                     FROM did_numbers n
                     JOIN did_clients c ON c.id = n.client_id
                     LEFT JOIN did_reservations r ON r.did_number_id = n.id
                    GROUP BY n.id ORDER BY n.did_number""",
                (now,),
            ).fetchall()]
            cdr = [dict(r) for r in conn.execute(
                """SELECT d.*, c.name AS client_name, c.currency
                     FROM did_cdr d JOIN did_clients c ON c.id = d.client_id
                    ORDER BY d.id DESC LIMIT 100"""
            ).fetchall()]
            outbound_routes = [dict(r) for r in conn.execute(
                """SELECT o.*, c.name AS client_name, c.currency,
                          COALESCE(NULLIF(g.name, ''), NULLIF(t.name, ''), o.provider_name) AS resolved_provider_name,
                          COALESCE(NULLIF(t.gateway_name, ''), NULLIF(g.gateway_name, ''), o.gateway_name) AS resolved_gateway_name,
                          COALESCE(NULLIF(t.ips, ''), NULLIF(g.ips, ''), o.route_ips) AS resolved_route_ips,
                          COALESCE(NULLIF(t.tech_prefix, ''), o.tech_prefix) AS resolved_tech_prefix,
                          COALESCE(t.cost_rate_cents, o.cost_rate_cents) AS resolved_cost_rate_cents,
                          COALESCE(NULLIF(t.billing_cycle, ''), o.cost_billing_cycle) AS resolved_cost_billing_cycle,
                          COALESCE(NULLIF(t.destination_name, ''), o.destination_name) AS resolved_destination_name,
                          COALESCE(SUM(CASE WHEN r.active = 1 AND r.expires_at > ? THEN 1 ELSE 0 END), 0) AS active_calls
                     FROM did_outbound_routes o
                     JOIN did_clients c ON c.id = o.client_id
                     LEFT JOIN terminators t ON t.id = o.terminator_id
                     LEFT JOIN termination_groups g ON g.id = t.gateway_group_id
                     LEFT JOIN did_outbound_reservations r ON r.route_id = o.id
                    GROUP BY o.id ORDER BY c.name, length(o.prefix), o.prefix""",
                (now,),
            ).fetchall()]
            existing_terminators = [dict(r) for r in conn.execute(
                """SELECT t.*, g.name AS group_name, g.ips AS group_ips,
                          g.gateway_name AS group_gateway_name, g.active AS group_active
                     FROM terminators t
                     LEFT JOIN termination_groups g ON g.id = t.gateway_group_id
                    WHERE t.active = 1 AND COALESCE(g.active, 1) = 1
                    ORDER BY COALESCE(g.name, t.name), t.destination_name, t.prefix"""
            ).fetchall()]
            outbound_cdr = [dict(r) for r in conn.execute(
                """SELECT d.*, c.name AS client_name, c.currency
                     FROM did_outbound_cdr d JOIN did_clients c ON c.id = d.client_id
                    ORDER BY d.id DESC LIMIT 100"""
            ).fetchall()]
            caller_ids = {}
            for row in conn.execute("SELECT client_id, caller_id FROM did_caller_ids ORDER BY client_id, caller_id").fetchall():
                caller_ids.setdefault(str(row["client_id"]), []).append(row["caller_id"])
            summary = dict(conn.execute(
                """SELECT
                    (SELECT COALESCE(SUM(balance_cents), 0) FROM did_clients) AS total_balance_cents,
                    (SELECT COUNT(*) FROM did_numbers WHERE active = 1) AS active_dids,
                    ((SELECT COUNT(*) FROM did_reservations WHERE active = 1 AND expires_at > ?) +
                     (SELECT COUNT(*) FROM did_outbound_reservations WHERE active = 1 AND expires_at > ?)) AS active_calls,
                    ((SELECT COALESCE(SUM(charged_cents), 0) FROM did_cdr WHERE date(ended_at) = date('now')) +
                     (SELECT COALESCE(SUM(charged_cents), 0) FROM did_outbound_cdr WHERE date(ended_at) = date('now'))) AS revenue_today_cents,
                    ((SELECT COALESCE(SUM(margin_cents), 0) FROM did_cdr WHERE date(ended_at) = date('now')) +
                     (SELECT COALESCE(SUM(margin_cents), 0) FROM did_outbound_cdr WHERE date(ended_at) = date('now'))) AS margin_today_cents""",
                (now, now),
            ).fetchone())
            conn.commit()
            return {
                "money_scale": db.MONEY_SCALE, "summary": summary, "clients": clients,
                "numbers": numbers, "cdr": cdr, "outbound_routes": outbound_routes,
                "outbound_cdr": outbound_cdr, "caller_ids": caller_ids,
                "existing_terminators": existing_terminators,
            }
        finally:
            conn.close()

    @app.post("/api/dids/clients", dependencies=main.ADMIN_WRITE_AUTH)
    def create_did_client(data: DidClientIn):
        conn = db.get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO did_clients
                       (name, balance_cents, credit_limit_cents, currency,
                        outbound_ips, outbound_tech_prefix, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    _clean_text(data.name, 120), data.balance_cents, data.credit_limit_cents,
                    data.currency.upper(), data.outbound_ips.strip(),
                    _digits(data.outbound_tech_prefix), int(data.active),
                ),
            )
            if data.balance_cents:
                conn.execute(
                    "INSERT INTO did_balance_transactions (client_id, amount_cents, kind, reference) VALUES (?, ?, 'opening', 'initial balance')",
                    (cur.lastrowid, data.balance_cents),
                )
            conn.commit()
            return {"id": cur.lastrowid}
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "DID-клиент с таким именем уже существует") from exc
        finally:
            conn.close()

    @app.patch("/api/dids/clients/{client_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def update_did_client(client_id: int, data: DidClientUpdate):
        fields = {k: v for k, v in data.dict().items() if v is not None}
        if not fields:
            raise HTTPException(400, "Нет полей для изменения")
        if "active" in fields:
            fields["active"] = int(fields["active"])
        if "currency" in fields:
            fields["currency"] = fields["currency"].upper()
        sql = ", ".join(f"{key} = ?" for key in fields)
        conn = db.get_conn()
        try:
            cur = conn.execute(f"UPDATE did_clients SET {sql} WHERE id = ?", (*fields.values(), client_id))
            if not cur.rowcount:
                raise HTTPException(404, "DID-клиент не найден")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @app.post("/api/dids/clients/{client_id}/topup", dependencies=main.ADMIN_WRITE_AUTH)
    def topup_did_client(client_id: int, data: DidTopupIn):
        if data.amount_cents == 0:
            raise HTTPException(422, "Сумма не может быть нулевой")
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE did_clients SET balance_cents = balance_cents + ? WHERE id = ?",
                (data.amount_cents, client_id),
            )
            if not cur.rowcount:
                raise HTTPException(404, "DID-клиент не найден")
            conn.execute(
                "INSERT INTO did_balance_transactions (client_id, amount_cents, kind, reference) VALUES (?, ?, 'topup', 'dashboard')",
                (client_id, data.amount_cents),
            )
            balance = conn.execute("SELECT balance_cents FROM did_clients WHERE id = ?", (client_id,)).fetchone()[0]
            conn.commit()
            return {"ok": True, "balance_cents": balance}
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/api/dids/numbers", dependencies=main.ADMIN_WRITE_AUTH)
    def create_did_number(data: DidNumberIn):
        number = _digits(data.did_number)
        if len(number) < 3:
            raise HTTPException(422, "Некорректный DID номер")
        conn = db.get_conn()
        try:
            client = conn.execute("SELECT id FROM did_clients WHERE id = ?", (data.client_id,)).fetchone()
            if client is None:
                raise HTTPException(404, "DID-клиент не найден")
            other_owner = conn.execute(
                "SELECT client_id FROM did_caller_ids WHERE caller_id = ? AND client_id <> ?",
                (number, data.client_id),
            ).fetchone()
            if other_owner is not None:
                raise HTTPException(409, "Этот номер уже закреплён как Caller ID другого DID-клиента")
            cur = conn.execute(
                """INSERT INTO did_numbers
                       (client_id, did_number, provider_name, provider_ips, destination,
                        backup_destination, sell_rate_cents, cost_rate_cents,
                        billing_cycle, max_channels, active, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.client_id, number, _clean_text(data.provider_name, 120), data.provider_ips.strip(),
                    data.destination.strip(), data.backup_destination.strip(), data.sell_rate_cents,
                    data.cost_rate_cents, db.normalize_billing_cycle(data.billing_cycle),
                    data.max_channels, int(data.active), data.notes.strip(),
                ),
            )
            conn.commit()
            return {"id": cur.lastrowid}
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Этот DID номер уже существует") from exc
        finally:
            conn.close()

    @app.patch("/api/dids/numbers/{number_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def update_did_number(number_id: int, data: DidNumberUpdate):
        fields = data.dict(exclude_unset=True)
        if not fields:
            raise HTTPException(400, "Нет полей для изменения")
        if "did_number" in fields:
            fields["did_number"] = _digits(fields["did_number"])
            if len(fields["did_number"]) < 3:
                raise HTTPException(422, "Некорректный DID номер")
        if "active" in fields:
            fields["active"] = int(fields["active"])
        if "billing_cycle" in fields:
            fields["billing_cycle"] = db.normalize_billing_cycle(fields["billing_cycle"])
        if "provider_name" in fields:
            fields["provider_name"] = _clean_text(fields["provider_name"], 120)
        for key in ("provider_ips", "destination", "backup_destination", "notes"):
            if key in fields:
                fields[key] = fields[key].strip()
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT client_id, did_number FROM did_numbers WHERE id = ?", (number_id,)
            ).fetchone()
            if current is None:
                raise HTTPException(404, "DID номер не найден")
            client_id = fields.get("client_id", current["client_id"])
            number = fields.get("did_number", current["did_number"])
            if conn.execute("SELECT 1 FROM did_clients WHERE id = ?", (client_id,)).fetchone() is None:
                raise HTTPException(404, "DID-клиент не найден")
            other_owner = conn.execute(
                "SELECT client_id FROM did_caller_ids WHERE caller_id = ? AND client_id <> ?",
                (number, client_id),
            ).fetchone()
            if other_owner is not None:
                raise HTTPException(409, "Этот номер уже закреплён как Caller ID другого DID-клиента")
            sql = ", ".join(f"{key} = ?" for key in fields)
            cur = conn.execute(f"UPDATE did_numbers SET {sql} WHERE id = ?", (*fields.values(), number_id))
            if not cur.rowcount:
                raise HTTPException(404, "DID номер не найден")
            conn.commit()
            return {"ok": True}
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, "Этот DID номер уже существует") from exc
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.delete("/api/dids/numbers/{number_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def delete_did_number(number_id: int):
        now = int(time.time())
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            number = conn.execute(
                "SELECT client_id, did_number FROM did_numbers WHERE id = ?", (number_id,)
            ).fetchone()
            if number is None:
                raise HTTPException(404, "DID номер не найден")
            active_calls = conn.execute(
                "SELECT COUNT(*) FROM did_reservations WHERE did_number_id = ? AND active = 1 AND expires_at > ?",
                (number_id, now),
            ).fetchone()[0]
            if active_calls:
                raise HTTPException(409, "Нельзя удалить DID: по нему сейчас идёт звонок")
            conn.execute("DELETE FROM did_reservations WHERE did_number_id = ?", (number_id,))
            conn.execute(
                "DELETE FROM did_caller_ids WHERE client_id = ? AND caller_id = ?",
                (number["client_id"], number["did_number"]),
            )
            conn.execute("DELETE FROM did_numbers WHERE id = ?", (number_id,))
            conn.commit()
            return {"ok": True}
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.put("/api/dids/clients/{client_id}/caller-ids", dependencies=main.ADMIN_WRITE_AUTH)
    def set_did_caller_ids(client_id: int, data: DidCallerIdsIn):
        raw_numbers = re.split(r"[\s,;]+", data.numbers or "")
        numbers = []
        for raw in raw_numbers:
            if not raw.strip():
                continue
            number = _digits(raw)
            if len(number) < 3 or len(number) > 15:
                raise HTTPException(422, f"Некорректный Caller ID: {raw}")
            if number not in numbers:
                numbers.append(number)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM did_clients WHERE id = ?", (client_id,)).fetchone() is None:
                raise HTTPException(404, "DID-клиент не найден")
            for number in numbers:
                did_owner = conn.execute(
                    "SELECT client_id FROM did_numbers WHERE did_number = ? AND client_id <> ?",
                    (number, client_id),
                ).fetchone()
                extra_owner = conn.execute(
                    "SELECT client_id FROM did_caller_ids WHERE caller_id = ? AND client_id <> ?",
                    (number, client_id),
                ).fetchone()
                if did_owner is not None or extra_owner is not None:
                    raise HTTPException(409, f"Caller ID {number} уже принадлежит другому DID-клиенту")
            conn.execute("DELETE FROM did_caller_ids WHERE client_id = ?", (client_id,))
            conn.executemany(
                "INSERT INTO did_caller_ids (client_id, caller_id) VALUES (?, ?)",
                [(client_id, number) for number in numbers],
            )
            conn.commit()
            return {"ok": True, "count": len(numbers)}
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/api/dids/outbound-routes", dependencies=main.ADMIN_WRITE_AUTH)
    def create_did_outbound_route(data: DidOutboundRouteIn):
        conn = db.get_conn()
        try:
            if conn.execute("SELECT 1 FROM did_clients WHERE id = ?", (data.client_id,)).fetchone() is None:
                raise HTTPException(404, "DID-клиент не найден")
            terminator = db.get_terminator(conn, data.terminator_id) if data.terminator_id else None
            if data.terminator_id and terminator is None:
                raise HTTPException(404, "Терминатор не найден")
            if terminator is not None and not terminator["active"]:
                raise HTTPException(409, "Выбранный терминатор выключен")
            if terminator is not None:
                group = db.get_termination_group(conn, terminator["gateway_group_id"])
                provider_name = (group["name"] if group is not None else "") or terminator["name"]
                destination_name = terminator["destination_name"]
                prefix = _digits(terminator["prefix"])
                gateway_name = (terminator["gateway_name"] or "") or ((group["gateway_name"] or "") if group else "")
                route_ips = (terminator["ips"] or "") or ((group["ips"] or "") if group else "")
                tech_prefix = terminator["tech_prefix"] or ""
                cost_rate_cents = terminator["cost_rate_cents"]
                cost_billing_cycle = db.normalize_billing_cycle(terminator["billing_cycle"])
            else:
                provider_name = _clean_text(data.provider_name, 120)
                destination_name = _clean_text(data.destination_name, 120)
                prefix = _digits(data.prefix)
                gateway_name = data.gateway_name.strip()
                route_ips = data.route_ips.strip()
                tech_prefix = _digits(data.tech_prefix)
                cost_rate_cents = data.cost_rate_cents
                cost_billing_cycle = db.normalize_billing_cycle(data.billing_cycle)
            if not prefix or not destination_name:
                raise HTTPException(422, "Выберите существующий терминатор или укажите направление и префикс")
            if not gateway_name and not db.split_ip_list(route_ips):
                raise HTTPException(422, "У терминатора нет gateway или IP поставщика")
            cur = conn.execute(
                """INSERT INTO did_outbound_routes
                       (client_id, terminator_id, provider_name, destination_name, prefix, gateway_name,
                        route_ips, tech_prefix, sell_rate_cents, cost_rate_cents,
                        billing_cycle, cost_billing_cycle, max_channels, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.client_id, data.terminator_id, provider_name, destination_name,
                    prefix, gateway_name, route_ips, tech_prefix, data.sell_rate_cents,
                    cost_rate_cents, db.normalize_billing_cycle(data.billing_cycle), cost_billing_cycle,
                    data.max_channels, int(data.active),
                ),
            )
            conn.commit()
            return {"id": cur.lastrowid}
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Такой исходящий маршрут уже существует у клиента") from exc
        finally:
            conn.close()

    @app.patch("/api/dids/outbound-routes/{route_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def update_did_outbound_route(route_id: int, data: DidOutboundRouteUpdate):
        fields = data.dict(exclude_unset=True)
        if not fields:
            raise HTTPException(400, "Нет полей для изменения")
        if "active" in fields:
            fields["active"] = int(fields["active"])
        for key in ("prefix", "tech_prefix"):
            if key in fields:
                fields[key] = _digits(fields[key])
        if "billing_cycle" in fields:
            fields["billing_cycle"] = db.normalize_billing_cycle(fields["billing_cycle"])
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM did_outbound_routes WHERE id = ?", (route_id,)).fetchone() is None:
                raise HTTPException(404, "Исходящий маршрут не найден")
            if "client_id" in fields and conn.execute(
                "SELECT 1 FROM did_clients WHERE id = ?", (fields["client_id"],)
            ).fetchone() is None:
                raise HTTPException(404, "DID-клиент не найден")
            if "terminator_id" in fields:
                terminator_id = fields["terminator_id"]
                terminator = db.get_terminator(conn, terminator_id) if terminator_id else None
                if terminator is None:
                    raise HTTPException(404, "Терминатор не найден")
                if not terminator["active"]:
                    raise HTTPException(409, "Выбранный терминатор выключен")
                group = db.get_termination_group(conn, terminator["gateway_group_id"])
                if group is not None and not group["active"]:
                    raise HTTPException(409, "Группа выбранного терминатора выключена")
                fields.update(
                    provider_name=((group["name"] if group else "") or terminator["name"]),
                    destination_name=terminator["destination_name"],
                    prefix=_digits(terminator["prefix"]),
                    gateway_name=((terminator["gateway_name"] or "") or ((group["gateway_name"] or "") if group else "")),
                    route_ips=((terminator["ips"] or "") or ((group["ips"] or "") if group else "")),
                    tech_prefix=terminator["tech_prefix"] or "",
                    cost_rate_cents=terminator["cost_rate_cents"],
                    cost_billing_cycle=db.normalize_billing_cycle(terminator["billing_cycle"]),
                )
            sql = ", ".join(f"{key} = ?" for key in fields)
            cur = conn.execute(f"UPDATE did_outbound_routes SET {sql} WHERE id = ?", (*fields.values(), route_id))
            if not cur.rowcount:
                raise HTTPException(404, "Исходящий маршрут не найден")
            conn.commit()
            return {"ok": True}
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, "Такой исходящий маршрут уже существует у клиента") from exc
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.delete("/api/dids/outbound-routes/{route_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def delete_did_outbound_route(route_id: int):
        now = int(time.time())
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM did_outbound_routes WHERE id = ?", (route_id,)).fetchone() is None:
                raise HTTPException(404, "Исходящий маршрут не найден")
            active_calls = conn.execute(
                "SELECT COUNT(*) FROM did_outbound_reservations WHERE route_id = ? AND active = 1 AND expires_at > ?",
                (route_id, now),
            ).fetchone()[0]
            if active_calls:
                raise HTTPException(409, "Нельзя удалить маршрут: по нему сейчас идёт звонок")
            conn.execute("DELETE FROM did_outbound_reservations WHERE route_id = ?", (route_id,))
            conn.execute("DELETE FROM did_outbound_routes WHERE id = ?", (route_id,))
            conn.commit()
            return {"ok": True}
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/api/dids/reserve", dependencies=main.API_AUTH)
    def did_reserve(data: DidReserveIn):
        return reserve_call(db, data)

    @app.post("/api/dids/finalize", dependencies=main.API_AUTH)
    def did_finalize(data: DidFinalizeIn):
        return finalize_call(db, data)

    @app.post("/api/dids/outbound/reserve", dependencies=main.API_AUTH)
    def did_outbound_reserve(data: DidOutboundReserveIn):
        return reserve_outbound_call(db, data)

    @app.post("/api/dids/outbound/finalize", dependencies=main.API_AUTH)
    def did_outbound_finalize(data: DidOutboundFinalizeIn):
        return finalize_outbound_call(db, data)
