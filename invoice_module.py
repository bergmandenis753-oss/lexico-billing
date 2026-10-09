"""Customer traffic and DID invoice reporting."""

import calendar
from datetime import date, timedelta
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse


MAX_PERIOD_DAYS = 731


def _period(date_from: str, date_to: str):
    try:
        start = date.fromisoformat(str(date_from or ""))
        end = date.fromisoformat(str(date_to or ""))
    except ValueError as exc:
        raise HTTPException(422, "Укажите корректные даты периода") from exc
    if end < start:
        raise HTTPException(422, "Дата окончания должна быть не раньше даты начала")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise HTTPException(422, "Период invoice не может превышать два года")
    return start, end


def _client(conn, table: str, client_id: int):
    row = conn.execute(
        f"SELECT id, name, currency FROM {table} WHERE id = ?", (client_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Клиент не найден")
    return dict(row)


def traffic_invoice(conn, client_id: int, date_from: str, date_to: str):
    start, end = _period(date_from, date_to)
    client = _client(conn, "clients", client_id)
    start_at = f"{start.isoformat()} 00:00:00"
    end_at = f"{(end + timedelta(days=1)).isoformat()} 00:00:00"
    summary = conn.execute(
        """SELECT COUNT(*) AS calls,
                  COALESCE(SUM(CASE WHEN billsec > 0 THEN 1 ELSE 0 END), 0) AS answered_calls,
                  COALESCE(SUM(billsec), 0) AS seconds,
                  COALESCE(SUM(charged_cents), 0) AS amount_cents
             FROM cdr
            WHERE client_id = ? AND started_at >= ? AND started_at < ?""",
        (client_id, start_at, end_at),
    ).fetchone()
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(terminator_destination_name, ''), 'Без направления') AS direction,
                  COUNT(*) AS calls,
                  COALESCE(SUM(CASE WHEN billsec > 0 THEN 1 ELSE 0 END), 0) AS answered_calls,
                  COALESCE(SUM(billsec), 0) AS seconds,
                  COALESCE(SUM(charged_cents), 0) AS amount_cents
             FROM cdr
            WHERE client_id = ? AND started_at >= ? AND started_at < ?
            GROUP BY COALESCE(NULLIF(terminator_destination_name, ''), 'Без направления')
            ORDER BY amount_cents DESC, direction""",
        (client_id, start_at, end_at),
    ).fetchall()
    return {
        "kind": "traffic",
        "client": client,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "summary": dict(summary),
        "rows": [dict(row) for row in rows],
    }


def _prorated_mrc(monthly_units: int, active_from: date, period_end: date):
    total = 0
    active_days = 0
    cursor = active_from
    while cursor <= period_end:
        days_in_month = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = date(cursor.year, cursor.month, days_in_month)
        segment_end = min(month_end, period_end)
        days = (segment_end - cursor).days + 1
        total += (int(monthly_units or 0) * days + days_in_month // 2) // days_in_month
        active_days += days
        cursor = segment_end + timedelta(days=1)
    return total, active_days


def did_invoice(conn, client_id: int, date_from: str, date_to: str):
    start, end = _period(date_from, date_to)
    client = _client(conn, "did_clients", client_id)
    numbers = conn.execute(
        """SELECT id, did_number, sold_on, sell_mrc_cents, sell_nrc_cents, active
             FROM did_numbers
            WHERE client_id = ? AND sold_on IS NOT NULL AND sold_on <> '' AND sold_on <= ?
            ORDER BY sold_on, did_number""",
        (client_id, end.isoformat()),
    ).fetchall()
    rows = []
    total_mrc = 0
    total_nrc = 0
    for number in numbers:
        try:
            sold_on = date.fromisoformat(number["sold_on"])
        except (TypeError, ValueError):
            continue
        active_from = max(start, sold_on)
        mrc, active_days = _prorated_mrc(number["sell_mrc_cents"], active_from, end)
        nrc = int(number["sell_nrc_cents"] or 0) if start <= sold_on <= end else 0
        total_mrc += mrc
        total_nrc += nrc
        rows.append(
            {
                "id": number["id"],
                "did_number": number["did_number"],
                "sold_on": sold_on.isoformat(),
                "active": int(number["active"] or 0),
                "active_days": active_days,
                "sell_mrc_cents": int(number["sell_mrc_cents"] or 0),
                "sell_nrc_cents": int(number["sell_nrc_cents"] or 0),
                "mrc_cents": mrc,
                "nrc_cents": nrc,
                "amount_cents": mrc + nrc,
            }
        )
    return {
        "kind": "dids",
        "client": client,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "summary": {
            "did_count": len(rows),
            "mrc_cents": total_mrc,
            "nrc_cents": total_nrc,
            "amount_cents": total_mrc + total_nrc,
        },
        "rows": rows,
    }


def install(app, main, db, base_path: Path):
    @app.get("/invoice", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def invoice_page(request: Request):
        return HTMLResponse(
            (base_path / "invoice.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/invoices/options", dependencies=main.ADMIN_AUTH)
    def invoice_options():
        conn = db.get_conn()
        try:
            return {
                "money_scale": db.MONEY_SCALE,
                "traffic_clients": [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, name, currency FROM clients "
                        "WHERE active = 1 ORDER BY name"
                    ).fetchall()
                ],
                "did_clients": [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, name, currency FROM did_clients "
                        "WHERE active = 1 ORDER BY name"
                    ).fetchall()
                ],
            }
        finally:
            conn.close()

    @app.get("/api/invoices/traffic", dependencies=main.ADMIN_AUTH)
    def get_traffic_invoice(client_id: int, date_from: str, date_to: str):
        conn = db.get_conn()
        try:
            return traffic_invoice(conn, client_id, date_from, date_to)
        finally:
            conn.close()

    @app.get("/api/invoices/dids", dependencies=main.ADMIN_AUTH)
    def get_did_invoice(client_id: int, date_from: str, date_to: str):
        conn = db.get_conn()
        try:
            return did_invoice(conn, client_id, date_from, date_to)
        finally:
            conn.close()
