from pathlib import Path
from collections import defaultdict
import json
import math
import os
import re
from typing import Optional
import urllib.error
import urllib.request

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import db
import main


app = main.app


def _split_ip_list(value):
    return [ip.strip() for ip in re.split(r"[\s,;]+", value or "") if ip.strip()]


db.split_ip_list = _split_ip_list


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _uses_current_money_scale(request: Request) -> bool:
    return request.headers.get("x-money-scale", "").strip() == str(db.MONEY_SCALE)


def _legacy_money(value):
    if value is None:
        return value
    return value / db.LEGACY_CENT_TO_MONEY_UNITS


def _legacy_rows(rows, fields):
    out = []
    for row in rows:
        item = dict(row)
        for field in fields:
            if field in item:
                item[field] = _legacy_money(item[field])
        out.append(item)
    return out


def _rows(rows):
    return [dict(row) for row in rows]


def _remove_routes(paths):
    app.router.routes = [
        route for route in app.router.routes
        if getattr(route, "path", "") not in paths
    ]


@app.middleware("http")
async def dashboard_scale_guard(request: Request, call_next):
    admin_write = (
        request.url.path.startswith("/api/")
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and not request.url.path.startswith((
            "/api/reserve",
            "/api/finalize",
            "/api/sip-guard",
            "/api/pcap-events",
            "/api/firewall-whitelist",
            "/api/ops/",
        ))
    )
    if admin_write and request.headers.get("authorization") and not _uses_current_money_scale(request):
        response = JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "Страница биллинга устарела. Обновите вкладку и повторите действие."},
        )
        return _no_store(response)

    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        _no_store(response)
    return response


_remove_routes({"/", "/api/dashboard-data", "/api/firewall-whitelist", "/api/finalize"})


class PcapEventIn(BaseModel):
    observed_at: str = ""
    direction: str = ""
    src_ip: str = ""
    src_port: str = ""
    dst_ip: str = ""
    dst_port: str = ""
    method: str = ""
    status_code: Optional[int] = None
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
    events: list[PcapEventIn] = Field(default_factory=list)


class AiAnalyzeIn(BaseModel):
    question: str = ""
    call_id: str = ""
    limit: int = Field(default=50, ge=1, le=200)


def _model_dict(item):
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return item.dict()


def _trim_text(value, limit=1000):
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "...[cut]"


def _pcap_event_dict(item):
    data = _model_dict(item)
    for key in (
        "observed_at",
        "direction",
        "src_ip",
        "src_port",
        "dst_ip",
        "dst_port",
        "method",
        "status_text",
        "call_id",
        "cseq",
        "from_user",
        "to_user",
        "request_uri",
        "user_agent",
        "reason",
    ):
        data[key] = _trim_text(data.get(key), 300)
    data["raw_summary"] = _trim_text(data.get("raw_summary"), 1600)
    if data.get("status_code") is not None:
        data["status_code"] = int(data["status_code"])
    return data


@app.post("/api/pcap-events", dependencies=main.API_AUTH)
def ingest_pcap_events(data: PcapEventsIn):
    events = [_pcap_event_dict(event) for event in data.events[:200]]
    saved = db.record_pcap_events(events)
    return {"ok": True, "saved": saved}


@app.get("/api/firewall-whitelist", dependencies=main.API_AUTH)
def firewall_whitelist():
    conn = db.get_conn()
    try:
        entries = []
        seen = set()

        def add_entries(raw_ips, **meta):
            for token in db.split_ip_list(raw_ips):
                if token in seen:
                    continue
                seen.add(token)
                entries.append({"ip": token, **meta})

        clients = conn.execute(
            "SELECT id, name, sip_ip FROM clients WHERE active = 1 ORDER BY id"
        ).fetchall()
        for row in clients:
            add_entries(row["sip_ip"], source="client", client_id=row["id"], client_name=row["name"])

        groups = conn.execute(
            "SELECT id, name, ips FROM termination_groups WHERE active = 1 ORDER BY id"
        ).fetchall()
        for row in groups:
            add_entries(row["ips"], source="termination_group", group_id=row["id"], group_name=row["name"])

        terminators = conn.execute(
            "SELECT t.id, t.name, t.ips, t.gateway_group_id, g.name AS group_name, g.ips AS group_ips "
            "FROM terminators t "
            "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
            "WHERE t.active = 1 ORDER BY t.id"
        ).fetchall()
        for row in terminators:
            add_entries(row["ips"], source="terminator", terminator_id=row["id"], terminator_name=row["name"])
            add_entries(
                row["group_ips"],
                source="terminator_group",
                terminator_id=row["id"],
                terminator_name=row["name"],
                group_id=row["gateway_group_id"],
                group_name=row["group_name"],
            )

        return {"ok": True, "entries": entries}
    finally:
        conn.close()


@app.post("/api/finalize", dependencies=main.API_AUTH)
def finalize(data: main.FinalizeIn):
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
        min_balance = db.minimum_client_balance_units(client)
        if new_balance < min_balance:
            max_charge = db.max_charge_units_for_client(client)
            print(
                f"[FINALIZE WARN] client={data.client_id} call={data.call_uuid} "
                f"charged={charged} exceeds available credit={max_charge}: clamp to limit {min_balance}"
            )
            charged = min(charged, max_charge)
            new_balance = client["balance_cents"] - charged

        margin = charged - cost
        conn.execute("UPDATE clients SET balance_cents = ? WHERE id = ?", (new_balance, data.client_id))
        termination_group_id, termination_group_name, supplier_debit = main._deduct_termination_group_balance(conn, data, cost)
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


def _public_row(row, fields):
    item = dict(row)
    return {field: item.get(field) for field in fields}


def _latest_diag_context(call_id="", limit=50):
    conn = db.get_conn()
    try:
        hits = conn.execute(
            "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
            "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cdr = conn.execute(
            "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
            "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
            "ORDER BY cd.id DESC LIMIT 30"
        ).fetchall()
        clients = conn.execute(
            "SELECT id, name, sip_ip, balance_cents, credit_limit_cents, currency, active FROM clients ORDER BY id"
        ).fetchall()
        terminators = conn.execute(
            "SELECT t.id, t.name, t.destination_name, t.prefix, t.tech_prefix, t.cost_rate_cents, "
            "t.active, g.name AS group_name, g.ips AS group_ips, g.gateway_name AS group_gateway_name "
            "FROM terminators t LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
            "ORDER BY t.active DESC, t.prefix, t.id"
        ).fetchall()

        chosen_call_id = (call_id or "").strip()
        if not chosen_call_id and hits:
            chosen_call_id = hits[0]["sip_call_id"] or ""

        if chosen_call_id:
            pcap = conn.execute(
                "SELECT * FROM pcap_events WHERE call_id = ? ORDER BY id LIMIT 240",
                (chosen_call_id,),
            ).fetchall()
            if len(pcap) < 5:
                more = conn.execute(
                    "SELECT * FROM pcap_events ORDER BY id DESC LIMIT 120",
                ).fetchall()
                seen_ids = {row["id"] for row in pcap}
                pcap = list(pcap) + [row for row in reversed(more) if row["id"] not in seen_ids]
        else:
            pcap = list(reversed(conn.execute(
                "SELECT * FROM pcap_events ORDER BY id DESC LIMIT 160",
            ).fetchall()))
    finally:
        conn.close()

    return {
        "money_scale": db.MONEY_SCALE,
        "selected_call_id": chosen_call_id,
        "sip_hits": [_public_row(row, (
            "id", "created_at", "call_uuid", "sip_ip", "sip_port", "clid", "destination",
            "client_name", "client_tech_prefix", "dial_destination", "provider_number",
            "gateway_name", "route_ip", "terminator_name", "terminator_destination_name",
            "terminator_prefix", "status", "stage", "reason", "max_seconds",
            "sell_rate_cents", "cost_rate_cents", "sip_call_id", "user_agent", "profile",
            "context",
        )) for row in hits],
        "cdr": [_public_row(row, (
            "id", "started_at", "call_uuid", "sip_ip", "clid", "destination",
            "client_name", "client_tech_prefix", "dial_destination", "provider_number",
            "gateway_name", "route_ip", "terminator_name", "terminator_destination_name",
            "terminator_prefix", "terminator_tech_prefix", "hangup_cause",
            "bridge_hangup_cause", "result", "billsec", "sell_rate_cents",
            "cost_rate_cents", "charged_cents", "margin_cents",
        )) for row in cdr],
        "pcap_events": [_public_row(row, (
            "id", "observed_at", "direction", "src_ip", "src_port", "dst_ip", "dst_port",
            "method", "status_code", "status_text", "call_id", "cseq", "from_user",
            "to_user", "request_uri", "user_agent", "reason", "raw_summary",
        )) for row in pcap],
        "clients": [_public_row(row, ("id", "name", "sip_ip", "balance_cents", "credit_limit_cents", "currency", "active")) for row in clients],
        "terminators": [_public_row(row, (
            "id", "name", "destination_name", "prefix", "tech_prefix", "cost_rate_cents",
            "active", "group_name", "group_ips", "group_gateway_name",
        )) for row in terminators],
    }


def _money_units(value):
    return f"{(int(value or 0) / db.MONEY_SCALE):.4f} USD"


def _cdr_for_hit(context, hit):
    call_uuid = hit.get("call_uuid") or ""
    if not call_uuid:
        return None
    for row in context["cdr"]:
        if row.get("call_uuid") == call_uuid:
            return row
    return None


def _events_for_hit(context, hit):
    call_id = hit.get("sip_call_id") or context.get("selected_call_id") or ""
    if not call_id:
        return context["pcap_events"]
    matched = [event for event in context["pcap_events"] if event.get("call_id") == call_id]
    return matched or context["pcap_events"]


def _short_pcap_summary(events):
    if not events:
        return "PCAP-событий по этому звонку пока нет."
    inbound_invites = [
        event for event in events
        if event.get("method") == "INVITE" and event.get("direction") == "in"
    ]
    outbound_invites = [
        event for event in events
        if event.get("method") == "INVITE" and event.get("direction") == "out"
    ]
    statuses = [
        f"{event.get('status_code')} {event.get('status_text') or ''}".strip()
        for event in events if event.get("status_code")
    ]
    methods = [event.get("method") for event in events if event.get("method")]
    parts = [f"PCAP: пакетов {len(events)}"]
    if inbound_invites:
        first = inbound_invites[0]
        parts.append(f"входящий INVITE от {first.get('src_ip')}:{first.get('src_port')}")
    if outbound_invites:
        targets = sorted({f"{event.get('dst_ip')}:{event.get('dst_port')}" for event in outbound_invites})
        parts.append("дальше отправили на " + ", ".join(targets[:4]))
    else:
        parts.append("исходящего INVITE дальше не видно")
    if statuses:
        parts.append("ответы: " + ", ".join(statuses[-8:]))
    if "CANCEL" in methods:
        parts.append("виден CANCEL от вызывающей стороны")
    if "BYE" in methods:
        parts.append("виден BYE")
    return "; ".join(parts) + "."


def _local_call_analysis(context, question):
    hits = context["sip_hits"]
    if not hits:
        return "Пока в биллинге нет SIP-хитов для разбора. Если звонок точно был, значит он не дошёл до нашего billing-скрипта или PCAP-сборщик ещё не установлен."

    hit = hits[0]
    cdr = _cdr_for_hit(context, hit)
    events = _events_for_hit(context, hit)
    lines = []
    lines.append(f"Последний хит: {hit.get('created_at')} от {hit.get('sip_ip')}:{hit.get('sip_port')}, номер {hit.get('destination')}, клиент {hit.get('client_name') or 'не определён'}.")

    status = hit.get("status") or ""
    stage = hit.get("stage") or ""
    reason = hit.get("reason") or ""
    if status in {"blocked", "rejected"}:
        if stage == "client_lookup":
            lines.append(f"Проблема на входе: IP не найден в whitelist. Причина: {reason}")
        elif stage == "client_rate":
            lines.append(f"Проблема в настройке продажи клиенту: для номера не найден тариф/направление. Причина: {reason}")
        elif stage == "terminator":
            lines.append(f"Проблема в маршрутизации: нет подходящего терминатора. Причина: {reason}")
        elif stage == "balance":
            lines.append(f"Проблема в балансе: {reason}")
        else:
            lines.append(f"Звонок отклонён на стадии {stage or 'неизвестно'}: {reason or 'причина не указана'}")
    else:
        lines.append(f"Наш сервер звонок принял, холд поставил и выбрал маршрут: {hit.get('terminator_name') or '—'} -> {hit.get('gateway_name') or hit.get('route_ip') or '—'}, номер дальше {hit.get('provider_number') or hit.get('dial_destination') or '—'}.")

    lines.append(_short_pcap_summary(events))

    status_codes = {event.get("status_code") for event in events if event.get("status_code")}
    has_200 = 200 in status_codes
    has_progress = bool({180, 183} & status_codes)
    has_outbound_invite = any(event.get("method") == "INVITE" and event.get("direction") == "out" for event in events)
    has_cancel = any(event.get("method") == "CANCEL" for event in events)

    if cdr:
        billsec = int(cdr.get("billsec") or 0)
        if billsec > 0:
            lines.append(
                f"В CDR есть connect: billsec={billsec}, списано {_money_units(cdr.get('charged_cents'))}, маржа {_money_units(cdr.get('margin_cents'))}."
            )
        else:
            cause = cdr.get("bridge_hangup_cause") or cdr.get("hangup_cause") or cdr.get("result") or "не указано"
            lines.append(f"В CDR connect не зафиксирован: billsec=0, код/причина {cause}. Баланс поэтому не списан.")
            if has_progress and not has_200:
                lines.append("Если клиент слышал гудки или voicemail, это похоже на early media: звук был до 200 OK, но billing правильно не считает это как ответ.")
            if "RECOVERY_ON_TIMER_EXPIRE" in cause or 408 in status_codes:
                lines.append("Похоже на таймаут на стороне терминатора или сети до него: мы отправили звонок, но нормального ответа/answer не дождались.")
            if "ORIGINATOR_CANCEL" in cause or has_cancel:
                lines.append("Инициатор отменил звонок до ответа, поэтому списания нет.")
    else:
        lines.append("CDR по этому хиту пока не найден. Если звонок уже завершён, надо проверить, дошёл ли billing.lua до финализации.")
        if has_200:
            lines.append("В PCAP виден 200 OK, но CDR нет: это подозрение на проблему finalize/answer-marker.")
        elif has_outbound_invite:
            lines.append("Исходящий INVITE виден, значит наш сервер пытался отправить звонок дальше.")

    if context["pcap_events"]:
        lines.append("Для точной картины смотри Call-ID: " + (hit.get("sip_call_id") or context.get("selected_call_id") or "—"))
    else:
        lines.append("PCAP-сборщик ещё не прислал пакеты, поэтому разбор пока основан только на SIP-хитах/CDR.")

    if question.strip():
        lines.append(f"По твоему вопросу: {question.strip()}")
    return "\n".join(lines)


def _extract_openai_text(payload):
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    chunks = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def _openai_analysis(context, question, fallback_text):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY не задан в Railway"

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    prompt = {
        "question": question or "Разбери последний звонок и скажи, где проблема.",
        "local_precheck": fallback_text,
        "data": context,
    }
    body = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": (
                    "Ты read-only помощник Lexico VoIP. Анализируй только переданный JSON: "
                    "sip_hits, cdr, pcap_events, clients, terminators. Ничего не изменяй и не обещай "
                    "изменять. Не выдумывай факты, если пакетов нет. Ответь по-русски коротко: "
                    "что произошло, дошло ли до нас, отправили ли дальше, был ли 200 OK/connect, "
                    "кто вероятнее отвечает за проблему, что проверить следующим шагом. "
                    "Деньги в полях *_cents на самом деле units 0.0001 USD."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False, default=str),
            },
        ],
        "max_output_tokens": 900,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = _extract_openai_text(payload)
        return text or None, "" if text else "OpenAI вернул пустой ответ"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return None, f"OpenAI HTTP {exc.code}: {_trim_text(detail, 500)}"
    except Exception as exc:
        return None, f"OpenAI недоступен: {exc}"


@app.post("/api/ai/analyze", dependencies=main.ADMIN_AUTH)
def ai_analyze(data: AiAnalyzeIn):
    context = _latest_diag_context(call_id=data.call_id, limit=data.limit)
    local_text = _local_call_analysis(context, data.question)
    ai_text, ai_error = _openai_analysis(context, data.question, local_text)
    latest_hit = context["sip_hits"][0] if context["sip_hits"] else {}
    return {
        "ok": True,
        "source": "openai" if ai_text else "local",
        "analysis": ai_text or local_text,
        "fallback": local_text,
        "ai_error": ai_error,
        "selected_call_id": context.get("selected_call_id") or latest_hit.get("sip_call_id") or "",
        "pcap_events_count": len(context["pcap_events"]),
    }


@app.get("/api/dashboard-data", dependencies=main.ADMIN_AUTH)
def dashboard_data(request: Request):
    conn = db.get_conn()
    try:
        clients = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
        groups = conn.execute("SELECT * FROM termination_groups ORDER BY name").fetchall()
        terminators = conn.execute(
            "SELECT t.*, g.name AS gateway_group_name, g.ips AS gateway_group_ips, "
            "g.gateway_name AS gateway_group_gateway_name, "
            "g.balance_cents AS gateway_group_balance_cents FROM terminators t "
            "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
            "ORDER BY t.prefix, t.active DESC, t.id"
        ).fetchall()
        client_rates = conn.execute(
            "SELECT cr.*, c.name AS client_name, t.name AS terminator_name, "
            "t.cost_rate_cents AS terminator_cost_rate_cents, "
            "t.tech_prefix AS terminator_tech_prefix, t.billing_cycle AS terminator_billing_cycle "
            "FROM client_rates cr JOIN clients c ON c.id = cr.client_id "
            "LEFT JOIN terminators t ON t.id = cr.terminator_id ORDER BY cr.client_id"
        ).fetchall()
        cdr = conn.execute(
            "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
            "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
            "ORDER BY cd.id DESC LIMIT 10"
        ).fetchall()
        sip_hits = conn.execute(
            "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
            "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT 50"
        ).fetchall()
        e164_directions = db.list_e164_countries(conn)
        total_balance = conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM clients").fetchone()["s"]
        margin_today = conn.execute("SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE date(started_at)=date('now')").fetchone()["s"]
        margin_month = conn.execute("SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE strftime('%Y-%m', started_at)=strftime('%Y-%m','now')").fetchone()["s"]
    finally:
        conn.close()

    if not _uses_current_money_scale(request):
        return {
            "money_scale": 100,
            "clients": _legacy_rows(clients, ("balance_cents", "credit_limit_cents")),
            "termination_groups": _rows(groups),
            "terminators": _legacy_rows(terminators, ("cost_rate_cents", "balance_cents")),
            "client_rates": _legacy_rows(client_rates, ("sell_rate_cents",)),
            "cdr": _legacy_rows(cdr, ("sell_rate_cents", "cost_rate_cents", "charged_cents", "margin_cents")),
            "sip_hits": _legacy_rows(sip_hits, ("sell_rate_cents", "cost_rate_cents")),
            "e164_directions": e164_directions,
            "summary": {
                "total_balance_cents": _legacy_money(total_balance),
                "margin_today_cents": _legacy_money(margin_today),
                "margin_month_cents": _legacy_money(margin_month),
            },
        }

    return {
        "money_scale": db.MONEY_SCALE,
        "clients": _rows(clients),
        "termination_groups": _rows(groups),
        "terminators": _rows(terminators),
        "client_rates": _rows(client_rates),
        "cdr": _rows(cdr),
        "sip_hits": _rows(sip_hits),
        "e164_directions": e164_directions,
        "summary": {
            "total_balance_cents": total_balance,
            "margin_today_cents": margin_today,
            "margin_month_cents": margin_month,
        },
    }


def _limited_rows(conn, query, params=()):
    return _rows(conn.execute(query, params).fetchall())


class OpsBalanceAdjustIn(BaseModel):
    client_id: int
    amount_cents: int
    reason: str = ""


@app.post("/api/ops/client-balance-adjust", dependencies=main.API_AUTH)
def ops_client_balance_adjust(data: OpsBalanceAdjustIn):
    if data.amount_cents == 0:
        raise HTTPException(400, "amount_cents must be non-zero")
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (data.client_id,)).fetchone()
        if client is None:
            conn.rollback()
            raise HTTPException(404, "Клиент не найден")
        new_balance = int(client["balance_cents"]) + int(data.amount_cents)
        min_balance = db.minimum_client_balance_units(client)
        if new_balance < min_balance:
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
            "credit_limit_cents": db.client_credit_limit_units(client),
        }
    finally:
        conn.close()


@app.get("/api/ops/diagnostics", dependencies=main.API_AUTH)
def ops_diagnostics():
    conn = db.get_conn()
    try:
        clients = _limited_rows(conn, "SELECT * FROM clients ORDER BY id")
        groups = _limited_rows(conn, "SELECT * FROM termination_groups ORDER BY name")
        terminators = _limited_rows(
            conn,
            "SELECT t.*, g.name AS gateway_group_name, g.ips AS gateway_group_ips, "
            "g.gateway_name AS gateway_group_gateway_name, "
            "g.balance_cents AS gateway_group_balance_cents FROM terminators t "
            "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
            "ORDER BY t.active DESC, t.prefix, t.id",
        )
        client_rates = _limited_rows(
            conn,
            "SELECT cr.*, c.name AS client_name, t.name AS terminator_name, "
            "t.cost_rate_cents AS terminator_cost_rate_cents, "
            "t.tech_prefix AS terminator_tech_prefix, t.billing_cycle AS terminator_billing_cycle "
            "FROM client_rates cr JOIN clients c ON c.id = cr.client_id "
            "LEFT JOIN terminators t ON t.id = cr.terminator_id "
            "ORDER BY cr.id DESC LIMIT 120",
        )
        cdr = _limited_rows(
            conn,
            "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
            "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
            "ORDER BY cd.id DESC LIMIT 50",
        )
        sip_hits = _limited_rows(
            conn,
            "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
            "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT 100",
        )
        pcap_events = _limited_rows(
            conn,
            "SELECT * FROM pcap_events ORDER BY id DESC LIMIT 200",
        )
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


@app.get("/", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
def dashboard(request: Request):
    html = Path("dashboard.html").read_text(encoding="utf-8")
    html = html.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">\n'
        '<meta http-equiv="Pragma" content="no-cache">\n'
        '<meta http-equiv="Expires" content="0">',
    )
    html = html.replace(
        "headers: {'Content-Type':'application/json'}",
        "headers: {'Content-Type':'application/json', 'X-Money-Scale': String(MONEY_SCALE)}",
    )
    html = html.replace(
        "fetch('/api/dashboard-data', {cache:'no-store'})",
        "fetch('/api/dashboard-data', {cache:'no-store', headers: {'X-Money-Scale': String(MONEY_SCALE)}})",
    )
    return _no_store(HTMLResponse(html))
