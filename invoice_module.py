"""Customer traffic and DID invoice reporting."""

import re
from datetime import date, timedelta
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


MAX_PERIOD_DAYS = 731


class DidSaleIn(BaseModel):
    client_id: int
    did_number: str = Field(min_length=3, max_length=32)
    sold_on: date
    sell_mrc_cents: int = Field(default=0, ge=0)
    sell_nrc_cents: int = Field(default=0, ge=0)
    cost_mrc_cents: int = Field(default=0, ge=0)
    cost_nrc_cents: int = Field(default=0, ge=0)


class DidSaleBatchIn(BaseModel):
    client_id: int
    did_numbers: list[str] = Field(min_length=1, max_length=500)
    sold_on: date
    sell_mrc_cents: int = Field(default=0, ge=0)
    sell_nrc_cents: int = Field(default=0, ge=0)
    cost_mrc_cents: int = Field(default=0, ge=0)
    cost_nrc_cents: int = Field(default=0, ge=0)


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
        f"SELECT id, name, currency, balance_cents FROM {table} WHERE id = ?", (client_id,)
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


def _next_month(value: date):
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _mrc_charge(monthly_units: int, sold_on: date, period_start: date, period_end: date):
    events = 1 if period_start <= sold_on <= period_end else 0
    cursor = _next_month(sold_on)
    while cursor <= period_end:
        if cursor >= period_start:
            events += 1
        cursor = _next_month(cursor)
    return int(monthly_units or 0) * events, events


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
        mrc, billing_events = _mrc_charge(number["sell_mrc_cents"], sold_on, start, end)
        nrc = int(number["sell_nrc_cents"] or 0) if start <= sold_on <= end else 0
        if not mrc and not nrc:
            continue
        total_mrc += mrc
        total_nrc += nrc
        rows.append(
            {
                "id": number["id"],
                "did_number": number["did_number"],
                "sold_on": sold_on.isoformat(),
                "active": int(number["active"] or 0),
                "billing_events": billing_events,
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


def save_did_sale(conn, data: DidSaleIn):
    client = conn.execute(
        "SELECT id FROM did_clients WHERE id = ?", (data.client_id,)
    ).fetchone()
    if client is None:
        raise HTTPException(404, "DID-клиент не найден")
    number = re.sub(r"\D", "", data.did_number or "")
    if len(number) < 3:
        raise HTTPException(422, "Некорректный DID номер")
    existing = conn.execute(
        "SELECT id, client_id FROM did_numbers WHERE did_number = ?", (number,)
    ).fetchone()
    if existing is not None and int(existing["client_id"]) != data.client_id:
        raise HTTPException(409, "Этот DID уже принадлежит другому клиенту")
    caller_id_owner = conn.execute(
        "SELECT client_id FROM did_caller_ids WHERE caller_id = ? AND client_id <> ?",
        (number, data.client_id),
    ).fetchone()
    if caller_id_owner is not None:
        raise HTTPException(409, "Этот номер закреплён за другим DID-клиентом")
    values = (
        data.sold_on.isoformat(), data.sell_mrc_cents, data.sell_nrc_cents,
        data.cost_mrc_cents, data.cost_nrc_cents,
    )
    if existing is not None:
        conn.execute(
            """UPDATE did_numbers
                  SET sold_on = ?, sell_mrc_cents = ?, sell_nrc_cents = ?,
                      cost_mrc_cents = ?, cost_nrc_cents = ?
                WHERE id = ?""",
            (*values, existing["id"]),
        )
        return {"id": existing["id"], "created": False, "did_number": number}
    cursor = conn.execute(
        """INSERT INTO did_numbers
               (client_id, did_number, destination, sold_on, sell_mrc_cents,
                sell_nrc_cents, cost_mrc_cents, cost_nrc_cents, active, notes)
           VALUES (?, ?, 'invoice-only', ?, ?, ?, ?, ?, 0,
                   'Создано из Invoice; настройте маршрут перед включением')""",
        (data.client_id, number, *values),
    )
    return {"id": cursor.lastrowid, "created": True, "did_number": number}


def save_did_sales(conn, data: DidSaleBatchIn):
    unique_numbers = []
    seen = set()
    for raw_number in data.did_numbers:
        number = re.sub(r"\D", "", raw_number or "")
        if number and number not in seen:
            seen.add(number)
            unique_numbers.append(number)
    if not unique_numbers:
        raise HTTPException(422, "Укажите хотя бы один DID номер")
    results = [
        save_did_sale(conn, DidSaleIn(
            client_id=data.client_id,
            did_number=number,
            sold_on=data.sold_on,
            sell_mrc_cents=data.sell_mrc_cents,
            sell_nrc_cents=data.sell_nrc_cents,
            cost_mrc_cents=data.cost_mrc_cents,
            cost_nrc_cents=data.cost_nrc_cents,
        ))
        for number in unique_numbers
    ]
    return {
        "count": len(results),
        "created": sum(1 for result in results if result["created"]),
        "updated": sum(1 for result in results if not result["created"]),
        "items": results,
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

    @app.post("/api/invoices/dids", dependencies=main.ADMIN_WRITE_AUTH)
    def add_invoice_did(data: DidSaleBatchIn):
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = save_did_sales(conn, data)
            conn.commit()
            return result
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()
