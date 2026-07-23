from pydantic import BaseModel

import admin_delete_patch
import admin_management_patch
import billing_ui_fix_patch
import client_route_isolation_patch
import client_portal
import main_compat
import multi_active_terminators_patch
import reserve_balance_patch


app = main_compat.app
main = main_compat.main
db = main_compat.db


class PcapEventIn(BaseModel):
    observed_at: str = ""
    direction: str = ""
    src_ip: str = ""
    src_port: str = ""
    dst_ip: str = ""
    dst_port: str = ""
    method: str = ""
    status_code: int | None = None
    status_text: str = ""
    call_id: str = ""
    cseq: str = ""
    from_user: str = ""
    to_user: str = ""
    request_uri: str = ""
    user_agent: str = ""
    reason: str = ""
    raw_summary: str = ""


class PcapEventsIn(BaseModel):
    events: list[PcapEventIn]


PCAP_COLUMNS = (
    "observed_at",
    "direction",
    "src_ip",
    "src_port",
    "dst_ip",
    "dst_port",
    "method",
    "status_code",
    "status_text",
    "call_id",
    "cseq",
    "from_user",
    "to_user",
    "request_uri",
    "user_agent",
    "reason",
    "raw_summary",
)


def _trim(value, limit=300):
    return str(value or "")[:limit]


def _rows(rows):
    return [dict(row) for row in rows]


def _limited_rows(conn, query, params=()):
    return _rows(conn.execute(query, params).fetchall())


@app.post("/api/pcap-events", dependencies=main.API_AUTH)
def ingest_pcap_events(data: PcapEventsIn):
    rows = []
    for event in data.events[:200]:
        item = event.model_dump() if hasattr(event, "model_dump") else event.dict()
        row = {}
        for key in PCAP_COLUMNS:
            if key == "status_code":
                row[key] = int(item[key]) if item.get(key) is not None else None
            elif key == "raw_summary":
                row[key] = _trim(item.get(key), 5000)
            else:
                row[key] = _trim(item.get(key), 300)
        rows.append(row)

    if not rows:
        return {"ok": True, "saved": 0}

    conn = db.get_conn()
    try:
        placeholders = ", ".join("?" for _ in PCAP_COLUMNS)
        conn.executemany(
            f"INSERT INTO pcap_events ({', '.join(PCAP_COLUMNS)}) VALUES ({placeholders})",
            [[row[column] for column in PCAP_COLUMNS] for row in rows],
        )
        conn.commit()
        return {"ok": True, "saved": len(rows)}
    finally:
        conn.close()


@app.get("/api/ops/diagnostics", dependencies=main.API_AUTH)
def ops_diagnostics(limit: int = 100, cdr_limit: int = 50, pcap_limit: int = 200):
    hit_limit = max(1, min(int(limit or 100), 1000))
    cdr_limit = max(1, min(int(cdr_limit or 50), 500))
    pcap_limit = max(1, min(int(pcap_limit or 200), 1000))
    conn = db.get_conn()
    try:
        clients = _limited_rows(conn, "SELECT * FROM clients ORDER BY id")
        groups = _limited_rows(conn, "SELECT * FROM termination_groups ORDER BY name")
        terminators = _limited_rows(
            conn,
            "SELECT t.*, g.name AS gateway_group_name, g.ips AS gateway_group_ips, "
            "g.gateway_name AS gateway_group_gateway_name FROM terminators t "
            "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
            "ORDER BY t.active DESC, t.prefix, t.id",
        )
        client_rates = _limited_rows(
            conn,
            "SELECT cr.*, c.name AS client_name, t.name AS terminator_name "
            "FROM client_rates cr JOIN clients c ON c.id = cr.client_id "
            "LEFT JOIN terminators t ON t.id = cr.terminator_id "
            "ORDER BY cr.id DESC LIMIT 500",
        )
        cdr = _limited_rows(
            conn,
            "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
            "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
            "ORDER BY cd.id DESC LIMIT ?",
            (cdr_limit,),
        )
        sip_hits = _limited_rows(
            conn,
            "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
            "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT ?",
            (hit_limit,),
        )
        pcap_events = _limited_rows(conn, "SELECT * FROM pcap_events ORDER BY id DESC LIMIT ?", (pcap_limit,))
        total_balance = conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM clients").fetchone()["s"]
        margin_today = conn.execute(
            "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE date(started_at)=date('now')"
        ).fetchone()["s"]
        margin_month = conn.execute(
            "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE strftime('%Y-%m', started_at)=strftime('%Y-%m','now')"
        ).fetchone()["s"]

        return {
            "ok": True,
            "money_scale": db.MONEY_SCALE,
            "summary": {
                "total_balance_cents": total_balance,
                "margin_today_cents": margin_today,
                "margin_month_cents": margin_month,
            },
            "clients": clients,
            "termination_groups": groups,
            "terminators": terminators,
            "client_rates": client_rates,
            "cdr": cdr,
            "sip_hits": sip_hits,
            "pcap_events": pcap_events,
        }
    finally:
        conn.close()


client_portal.install(app, main, db)
admin_management_patch.install(app, main, db, main_compat)
admin_delete_patch.install(app, main, db)
billing_ui_fix_patch.install(app, main, db)
reserve_balance_patch.install(app, main, db)
multi_active_terminators_patch.install(app, main, db)
client_route_isolation_patch.install(app, main, db)
