import os

from fastapi import Request
from pydantic import BaseModel

import admin_delete_patch
import admin_management_patch
import billing_ui_fix_patch
import client_route_isolation_patch
import client_telegram_alerts_patch
import client_portal
from credit_limit_common import ensure_schema
import credit_limit_calls
import credit_limit_patch
import low_balance_settings_patch
import main_compat
import multi_active_terminators_patch
import reserve_balance_patch
import route_pool_patch
import telegram_balance_patch


app = main_compat.app
main = main_compat.main
db = main_compat.db

MANUAL_MARGIN_ADJUSTMENT = 663900
MANUAL_MARGIN_DAY = "2026-07-24"
MANUAL_MARGIN_MONTH = "2026-07"
BUILD_MARKER = "ops-supplier-dashboard-reconcile-2026-08-20"


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


class SupplierBalanceReconcileIn(BaseModel):
    terminator_name: str = ""
    started_after: str = ""
    limit: int = 200


class _FinalizeRowData:
    def __init__(self, row):
        self.call_uuid = row["call_uuid"] or ""
        self.terminator_id = row["terminator_id"]
        self.terminator_name = row["terminator_name"] or ""
        self.gateway_name = row["gateway_name"] or ""
        self.route_ip = row["route_ip"] or ""


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


def _reconcile_missing_supplier_balances(limit=200, terminator_name="", started_after="", auto_window_hours=None):
    limit = max(1, min(int(limit or 200), 1000))
    terminator_name = (terminator_name or "").strip()
    started_after = (started_after or "").strip()
    where = [
        "COALESCE(supplier_balance_debit_cents, 0) = 0",
        "COALESCE(billsec, 0) > 0",
        "COALESCE(cost_rate_cents, 0) > 0",
    ]
    params = []
    if terminator_name:
        where.append("(terminator_name = ? OR termination_group_name = ?)")
        params.extend([terminator_name, terminator_name])
    if started_after:
        where.append("started_at >= ?")
        params.append(started_after)
    elif auto_window_hours:
        try:
            hours = int(auto_window_hours)
        except Exception:
            hours = 0
        if hours > 0:
            where.append("started_at >= datetime('now', ?)")
            params.append(f"-{min(hours, 720)} hours")

    ensure_schema(db)
    conn = db.get_conn()
    fixed = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT * FROM cdr WHERE {' AND '.join(where)} ORDER BY id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
        for row in rows:
            cost_units = db.charge_units(
                int(row["cost_rate_cents"] or 0),
                int(row["billsec"] or 0),
                db.normalize_billing_cycle(row["cost_billing_cycle"] or "60/60"),
            )
            group_id, group_name, supplier_debit = credit_limit_calls._deduct_termination_group_balance(
                conn,
                db,
                _FinalizeRowData(row),
                cost_units,
            )
            if not supplier_debit:
                continue
            updated = conn.execute(
                "UPDATE cdr SET termination_group_id = ?, termination_group_name = ?, "
                "supplier_balance_debit_cents = ? WHERE id = ? "
                "AND COALESCE(supplier_balance_debit_cents, 0) = 0",
                (group_id, group_name, supplier_debit, row["id"]),
            ).rowcount
            if not updated:
                continue
            fixed.append(
                {
                    "cdr_id": row["id"],
                    "call_uuid": row["call_uuid"],
                    "terminator_name": row["terminator_name"],
                    "termination_group_id": group_id,
                    "termination_group_name": group_name,
                    "supplier_balance_debit_cents": supplier_debit,
                }
            )
        conn.commit()
        return {
            "ok": True,
            "fixed": len(fixed),
            "total_supplier_balance_debit_cents": sum(item["supplier_balance_debit_cents"] for item in fixed),
            "rows": fixed,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    low_balance_threshold_cents = low_balance_settings_patch.get_low_balance_threshold_cents(db)
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
        today = conn.execute("SELECT date('now') AS d").fetchone()["d"]
        month = conn.execute("SELECT strftime('%Y-%m','now') AS m").fetchone()["m"]
        if today == MANUAL_MARGIN_DAY:
            margin_today -= MANUAL_MARGIN_ADJUSTMENT
        if month == MANUAL_MARGIN_MONTH:
            margin_month -= MANUAL_MARGIN_ADJUSTMENT

        return {
            "ok": True,
            "build_marker": BUILD_MARKER,
            "finalize_routes": [
                getattr(route, "name", "")
                for route in app.router.routes
                if getattr(route, "path", "") == "/api/finalize"
            ],
            "money_scale": db.MONEY_SCALE,
            "low_balance_threshold_cents": low_balance_threshold_cents,
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


@app.post("/api/ops/reconcile-supplier-balances", dependencies=main.API_AUTH)
def reconcile_supplier_balances(data: SupplierBalanceReconcileIn):
    return _reconcile_missing_supplier_balances(
        limit=data.limit,
        terminator_name=data.terminator_name,
        started_after=data.started_after,
    )


def _remove_route(path, methods):
    wanted = {method.upper() for method in methods}
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", "") == path
            and set(getattr(route, "methods", set()) or set()) & wanted
        )
    ]


client_portal.install(app, main, db)
admin_management_patch.install(app, main, db, main_compat)
admin_delete_patch.install(app, main, db)
billing_ui_fix_patch.install(app, main, db)
route_pool_patch.install(db)
reserve_balance_patch.install(app, main, db)
multi_active_terminators_patch.install(app, main, db)
client_route_isolation_patch.install(app, main, db)
telegram_balance_patch.install(app, main, db)
credit_limit_patch.install(app, main, db)
client_telegram_alerts_patch.install(app, main, db)
low_balance_settings_patch.install(app, main, db)

# Keep billing call routes last: some optional patches also touch API routes,
# and supplier balance deduction must run on every finalized call.
credit_limit_patch.install(app, main, db)


def _install_supplier_dashboard_reconcile():
    original_dashboard_data = None
    for route in app.router.routes:
        if (
            getattr(route, "path", "") == "/api/dashboard-data"
            and "GET" in set(getattr(route, "methods", set()) or set())
        ):
            original_dashboard_data = getattr(route, "endpoint", None)
            break

    _remove_route("/api/dashboard-data", {"GET"})

    @app.get("/api/dashboard-data", dependencies=main.ADMIN_AUTH)
    def dashboard_data_with_supplier_reconcile(request: Request):
        try:
            hours = os.getenv("SUPPLIER_BALANCE_AUTO_RECONCILE_HOURS", "0")
            _reconcile_missing_supplier_balances(limit=1000, auto_window_hours=hours)
        except Exception as exc:
            print(f"supplier balance auto reconcile skipped: {exc}")
        if original_dashboard_data is not None:
            return original_dashboard_data(request)
        return main_compat.dashboard_data(request)

    return dashboard_data_with_supplier_reconcile


_install_supplier_dashboard_reconcile()
