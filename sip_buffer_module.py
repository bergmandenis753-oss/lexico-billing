"""Standalone SIP buffer control plane.

This module deliberately does not import or query the billing database.  It
stores a very small routing configuration in a separate SQLite file.  The
dedicated FreeSWITCH edge periodically downloads that configuration and then
handles calls locally, without an HTTP request in the call path.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sqlite3
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


DEFAULT_NODE_KEY = "salesrender-edge-1"


def database_path(billing_db_module) -> Path:
    configured = (os.getenv("SIP_BUFFER_DB_PATH") or "").strip()
    if configured:
        return Path(configured)
    return Path(billing_db_module.DB_PATH).with_name("sip_buffer.db")


def get_conn(billing_db_module):
    path = database_path(billing_db_module)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(billing_db_module) -> None:
    conn = get_conn(billing_db_module)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sip_buffer_nodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                node_key    TEXT NOT NULL UNIQUE,
                public_ip   TEXT NOT NULL DEFAULT '',
                notes       TEXT NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sip_buffer_routes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id          INTEGER NOT NULL REFERENCES sip_buffer_nodes(id),
                name             TEXT NOT NULL,
                originator_name  TEXT NOT NULL,
                originator_ips   TEXT NOT NULL,
                inbound_number   TEXT NOT NULL,
                terminator_name  TEXT NOT NULL,
                destination_uri  TEXT NOT NULL,
                notes            TEXT NOT NULL DEFAULT '',
                active           INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_sip_buffer_routes_node
                ON sip_buffer_routes(node_id, active);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sip_buffer_routes_number
                ON sip_buffer_routes(node_id, inbound_number);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _clean(value, limit=500):
    return str(value or "").strip()[:limit]


def _node_key(value):
    value = _clean(value, 80).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", value):
        raise HTTPException(422, "Ключ сервера: только a-z, 0-9, _ и -")
    return value


def _originator_ips(value):
    tokens = [item for item in re.split(r"[\s,;]+", _clean(value, 2000)) if item]
    if not tokens:
        raise HTTPException(422, "Укажите хотя бы один IP originator")
    normalized = []
    for token in tokens:
        try:
            address = str(ipaddress.ip_address(token))
        except ValueError as exc:
            raise HTTPException(422, f"Некорректный IP originator: {token}") from exc
        if address not in normalized:
            normalized.append(address)
    return "\n".join(normalized)


def _number(value):
    digits = re.sub(r"\D", "", _clean(value, 80))
    if len(digits) < 5 or len(digits) > 18:
        raise HTTPException(422, "Укажите корректный входящий номер")
    return digits


def _destination_uri(value):
    uri = _clean(value, 1000)
    # SalesRender documentation sometimes displays placeholders as
    # sip:{plugin_hash}:{domain}.  FreeSWITCH needs the standard SIP user@host
    # form on the wire.
    placeholder = re.fullmatch(r"sips?:\{?([^{}:@\s]+)\}?:\{?([^{}:@\s]+)\}?", uri, re.I)
    if placeholder:
        uri = f"sip:{placeholder.group(1)}@{placeholder.group(2)}"
    if not re.fullmatch(r"sips?:[^@\s]+@[A-Za-z0-9.-]+(?::\d{1,5})?", uri, re.I):
        raise HTTPException(422, "SIP URI должен иметь формат sip:login@domain")
    return uri


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    node_key: str = Field(min_length=2, max_length=80)
    public_ip: str = ""
    notes: str = ""
    active: bool = True


class RouteIn(BaseModel):
    node_id: int
    name: str = Field(min_length=1, max_length=160)
    originator_name: str = Field(min_length=1, max_length=160)
    originator_ips: str = Field(min_length=1, max_length=2000)
    inbound_number: str = Field(min_length=5, max_length=80)
    terminator_name: str = Field(min_length=1, max_length=160)
    destination_uri: str = Field(min_length=5, max_length=1000)
    notes: str = ""
    active: bool = False


def _payload(conn):
    nodes = [dict(row) for row in conn.execute(
        "SELECT * FROM sip_buffer_nodes ORDER BY active DESC, name, id"
    ).fetchall()]
    routes = [dict(row) for row in conn.execute(
        """SELECT r.*, n.name AS node_name, n.node_key, n.public_ip AS node_public_ip
             FROM sip_buffer_routes r
             JOIN sip_buffer_nodes n ON n.id = r.node_id
            ORDER BY r.active DESC, r.name, r.id"""
    ).fetchall()]
    for node in nodes:
        node["active"] = bool(node["active"])
    for route in routes:
        route["active"] = bool(route["active"])
        route["originator_ip_list"] = route["originator_ips"].splitlines()
        route["bridge_target"] = f"sofia/external/{route['destination_uri']}"
    return {"nodes": nodes, "routes": routes, "database": "isolated"}


def install(app, main, billing_db_module, base_path: Path) -> None:
    init_schema(billing_db_module)

    @app.on_event("startup")
    def _sip_buffer_startup():
        init_schema(billing_db_module)

    @app.get("/sip-crm", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def sip_buffer_page(request: Request):
        html = (base_path / "sip_crm.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/sip-buffer", dependencies=main.ADMIN_AUTH)
    def sip_buffer_data():
        conn = get_conn(billing_db_module)
        try:
            return _payload(conn)
        finally:
            conn.close()

    @app.post("/api/sip-buffer/nodes", dependencies=main.ADMIN_WRITE_AUTH)
    def create_node(data: NodeIn):
        conn = get_conn(billing_db_module)
        try:
            cur = conn.execute(
                """INSERT INTO sip_buffer_nodes
                       (name, node_key, public_ip, notes, active, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (_clean(data.name, 120), _node_key(data.node_key), _clean(data.public_ip, 160),
                 _clean(data.notes, 2000), int(data.active)),
            )
            conn.commit()
            return {"ok": True, "id": cur.lastrowid}
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Сервер с таким ключом уже существует") from exc
        finally:
            conn.close()

    @app.put("/api/sip-buffer/nodes/{node_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def update_node(node_id: int, data: NodeIn):
        conn = get_conn(billing_db_module)
        try:
            cur = conn.execute(
                """UPDATE sip_buffer_nodes
                      SET name=?, node_key=?, public_ip=?, notes=?, active=?, updated_at=datetime('now')
                    WHERE id=?""",
                (_clean(data.name, 120), _node_key(data.node_key), _clean(data.public_ip, 160),
                 _clean(data.notes, 2000), int(data.active), node_id),
            )
            if not cur.rowcount:
                raise HTTPException(404, "Буферный сервер не найден")
            if not data.active:
                conn.execute("UPDATE sip_buffer_routes SET active=0 WHERE node_id=?", (node_id,))
            conn.commit()
            return {"ok": True}
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, "Сервер с таким ключом уже существует") from exc
        finally:
            conn.close()

    @app.delete("/api/sip-buffer/nodes/{node_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def delete_node(node_id: int):
        conn = get_conn(billing_db_module)
        try:
            if conn.execute("SELECT 1 FROM sip_buffer_routes WHERE node_id=?", (node_id,)).fetchone():
                raise HTTPException(409, "Сначала удалите маршруты этого сервера")
            cur = conn.execute("DELETE FROM sip_buffer_nodes WHERE id=?", (node_id,))
            if not cur.rowcount:
                raise HTTPException(404, "Буферный сервер не найден")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def save_route(conn, data: RouteIn, route_id=None):
        node = conn.execute("SELECT active FROM sip_buffer_nodes WHERE id=?", (data.node_id,)).fetchone()
        if node is None:
            raise HTTPException(404, "Буферный сервер не найден")
        if data.active and not node["active"]:
            raise HTTPException(422, "Сначала включите буферный сервер")
        values = (
            data.node_id, _clean(data.name, 160), _clean(data.originator_name, 160),
            _originator_ips(data.originator_ips), _number(data.inbound_number),
            _clean(data.terminator_name, 160), _destination_uri(data.destination_uri),
            _clean(data.notes, 3000), int(data.active),
        )
        if route_id is None:
            cur = conn.execute(
                """INSERT INTO sip_buffer_routes
                       (node_id,name,originator_name,originator_ips,inbound_number,
                        terminator_name,destination_uri,notes,active,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
                values,
            )
        else:
            cur = conn.execute(
                """UPDATE sip_buffer_routes
                      SET node_id=?,name=?,originator_name=?,originator_ips=?,inbound_number=?,
                          terminator_name=?,destination_uri=?,notes=?,active=?,updated_at=datetime('now')
                    WHERE id=?""",
                values + (route_id,),
            )
            if not cur.rowcount:
                raise HTTPException(404, "Маршрут не найден")
        return cur

    @app.post("/api/sip-buffer/routes", dependencies=main.ADMIN_WRITE_AUTH)
    def create_route(data: RouteIn):
        conn = get_conn(billing_db_module)
        try:
            cur = save_route(conn, data)
            conn.commit()
            return {"ok": True, "id": cur.lastrowid}
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, "Для этого номера на сервере уже есть маршрут") from exc
        finally:
            conn.close()

    @app.put("/api/sip-buffer/routes/{route_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def update_route(route_id: int, data: RouteIn):
        conn = get_conn(billing_db_module)
        try:
            save_route(conn, data, route_id)
            conn.commit()
            return {"ok": True}
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, "Для этого номера на сервере уже есть маршрут") from exc
        finally:
            conn.close()

    @app.delete("/api/sip-buffer/routes/{route_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def delete_route(route_id: int):
        conn = get_conn(billing_db_module)
        try:
            cur = conn.execute("DELETE FROM sip_buffer_routes WHERE id=?", (route_id,))
            if not cur.rowcount:
                raise HTTPException(404, "Маршрут не найден")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    # Keep the existing edge URL so the already-installed sync service needs no
    # restart or credential change.  Its data now comes only from sip_buffer.db.
    @app.get("/api/freeswitch/sip-crm-config/{node_key}", dependencies=main.API_AUTH)
    def edge_config(node_key: str):
        conn = get_conn(billing_db_module)
        try:
            node = conn.execute(
                "SELECT id,name,node_key,public_ip FROM sip_buffer_nodes WHERE node_key=? AND active=1",
                (_node_key(node_key),),
            ).fetchone()
            if node is None:
                raise HTTPException(404, "Активный буферный сервер не найден")
            rows = conn.execute(
                """SELECT id,name,originator_name,originator_ips,inbound_number,
                          terminator_name,destination_uri,updated_at
                     FROM sip_buffer_routes
                    WHERE node_id=? AND active=1
                    ORDER BY inbound_number,id""",
                (node["id"],),
            ).fetchall()
            routes = []
            for row in rows:
                route = dict(row)
                route["originator_ips"] = route["originator_ips"].splitlines()
                route["bridge_target"] = f"sofia/external/{route['destination_uri']}"
                routes.append(route)
            return {"node": dict(node), "routes": routes, "database": "isolated"}
        finally:
            conn.close()
