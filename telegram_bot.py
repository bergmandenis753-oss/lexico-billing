import json
import os
import textwrap
import urllib.error
import urllib.request
from typing import Optional

from fastapi import Header, HTTPException, Request, status
from pydantic import BaseModel


class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[dict] = None
    edited_message: Optional[dict] = None
    callback_query: Optional[dict] = None


def _trim(value, limit=300):
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _token():
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _webhook_secret():
    return (os.getenv("TELEGRAM_WEBHOOK_SECRET") or os.getenv("API_SECRET_KEY") or "").strip()


def _allowed_chat_ids():
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def _chat_allowed(chat_id):
    if os.getenv("TELEGRAM_ALLOW_ALL", "").strip().lower() in {"1", "true", "yes"}:
        return True
    allowed = _allowed_chat_ids()
    return bool(allowed) and str(chat_id) in allowed


def _telegram_api(method, payload):
    token = _token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {_trim(detail, 600)}")


def _send_message(chat_id, text):
    text = str(text or "").strip() or "Нет данных."
    chunks = textwrap.wrap(text, width=3500, replace_whitespace=False, drop_whitespace=False)
    for chunk in chunks or [text]:
        _telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
        )


def _rows(rows):
    return [dict(row) for row in rows]


def _money(value, scale=10000):
    return f"{(int(value or 0) / scale):.4f}"


def _latest_context(db, limit=50):
    conn = db.get_conn()
    try:
        clients = _rows(conn.execute("SELECT * FROM clients ORDER BY id").fetchall())
        groups = _rows(conn.execute("SELECT * FROM termination_groups ORDER BY name").fetchall())
        terminators = _rows(
            conn.execute(
                "SELECT t.*, g.name AS gateway_group_name, g.ips AS gateway_group_ips, "
                "g.gateway_name AS gateway_group_gateway_name FROM terminators t "
                "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
                "ORDER BY t.active DESC, t.prefix, t.id"
            ).fetchall()
        )
        rates = _rows(
            conn.execute(
                "SELECT cr.*, c.name AS client_name, t.name AS terminator_name "
                "FROM client_rates cr JOIN clients c ON c.id = cr.client_id "
                "LEFT JOIN terminators t ON t.id = cr.terminator_id ORDER BY cr.id DESC LIMIT 80"
            ).fetchall()
        )
        sip_hits = _rows(
            conn.execute(
                "SELECT * FROM sip_hits ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit or 50), 100)),),
            ).fetchall()
        )
        cdr = _rows(conn.execute("SELECT * FROM cdr ORDER BY id DESC LIMIT 20").fetchall())
        pcap = _rows(conn.execute("SELECT * FROM pcap_events ORDER BY id DESC LIMIT 120").fetchall())
        return {
            "clients": clients,
            "termination_groups": groups,
            "terminators": terminators,
            "client_rates": rates,
            "sip_hits": sip_hits,
            "cdr": cdr,
            "pcap_events": pcap,
        }
    finally:
        conn.close()


def _format_hit(hit):
    if not hit:
        return "SIP хитов пока нет."
    ip_port = f"{hit.get('sip_ip') or '-'}:{hit.get('sip_port') or '-'}"
    route = hit.get("gateway_name") or hit.get("route_ip") or "-"
    reason = " · ".join(
        str(x) for x in (hit.get("stage"), hit.get("reason")) if str(x or "").strip()
    )
    return "\n".join(
        [
            f"Время: {hit.get('created_at') or '-'}",
            f"IP:порт: {ip_port}",
            f"CLID: {hit.get('clid') or '-'}",
            f"Входящий номер: {hit.get('destination') or '-'}",
            f"Клиент: {hit.get('client_name') or '-'}",
            f"Номер дальше: {hit.get('dial_destination') or hit.get('provider_number') or '-'}",
            f"Статус: {hit.get('status') or '-'}",
            f"Причина: {reason or '-'}",
            f"Терминатор: {hit.get('terminator_name') or '-'}",
            f"Куда шлём: {route}",
            f"Call-ID: {hit.get('sip_call_id') or hit.get('call_uuid') or '-'}",
        ]
    )


def _pcap_for_hit(context, hit):
    call_ids = {hit.get("sip_call_id"), hit.get("call_uuid")}
    call_ids = {str(x) for x in call_ids if x}
    if call_ids:
        events = [event for event in context["pcap_events"] if str(event.get("call_id") or "") in call_ids]
        if events:
            return events[:30]
    ip = str(hit.get("sip_ip") or "")
    created = str(hit.get("created_at") or "")[:16]
    if ip and created:
        return [
            event for event in context["pcap_events"]
            if (event.get("src_ip") == ip or event.get("dst_ip") == ip)
            and str(event.get("created_at") or "").startswith(created[:13])
        ][:30]
    return context["pcap_events"][:12]


def _pcap_summary(events):
    if not events:
        return "PCAP/SIP пакетов по этому хиту не найдено."
    lines = ["SIP-пакеты:"]
    for event in events[:18]:
        if event.get("status_code"):
            what = f"SIP {event.get('status_code')} {event.get('status_text') or ''}".strip()
        else:
            what = event.get("method") or "SIP"
        src = f"{event.get('src_ip') or '?'}:{event.get('src_port') or '?'}"
        dst = f"{event.get('dst_ip') or '?'}:{event.get('dst_port') or '?'}"
        lines.append(f"{event.get('observed_at') or event.get('created_at') or '-'} | {src} -> {dst} | {what}")
    return "\n".join(lines)


def _latest_cdr_summary(context):
    if not context["cdr"]:
        return "CDR пока пустой."
    row = context["cdr"][0]
    return "\n".join(
        [
            f"Последний CDR: {row.get('started_at') or '-'}",
            f"Клиент ID: {row.get('client_id')}",
            f"Номер: {row.get('destination') or '-'}",
            f"Терминатор: {row.get('terminator_name') or row.get('gateway_name') or '-'}",
            f"Billsec: {row.get('billsec')}",
            f"Списано: {_money(row.get('charged_cents'))}",
            f"Маржа: {_money(row.get('margin_cents'))}",
            f"Отбой: {row.get('result') or row.get('bridge_hangup_cause') or row.get('hangup_cause') or '-'}",
        ]
    )


def _local_analysis(context, question=""):
    if not context["sip_hits"]:
        return "Пока не вижу SIP INVITE в таблице хитов."

    hit = context["sip_hits"][0]
    events = _pcap_for_hit(context, hit)
    lines = ["Разбор последнего хита:", _format_hit(hit), ""]

    stage = str(hit.get("stage") or "")
    status_value = str(hit.get("status") or "")
    reason = str(hit.get("reason") or "")
    if status_value == "rejected":
        if stage == "client_lookup":
            lines.append("Вывод: IP отправителя не найден среди активных оригинаторов.")
        elif stage == "client_rate":
            lines.append("Вывод: клиент найден, но нет открытого тарифа/роута под этот номер или техпрефикс.")
        elif stage == "terminator":
            lines.append("Вывод: продажный роут есть, но активный терминатор под направление не найден.")
        elif stage == "balance":
            lines.append("Вывод: не хватает свободного баланса или баланс занят активными холдами.")
        else:
            lines.append(f"Вывод: звонок отклонён на стороне нашего switch. Причина: {reason or stage or '-'}")
    elif status_value in {"allowed", "answered", "failed"}:
        lines.append("Вывод: наш switch принял звонок и попытался отправить его дальше.")
        if "408" in reason:
            lines.append("408 обычно значит, что дальше по маршруту не пришёл ответ вовремя.")
        if "503" in reason or "SERVICE_UNAVAILABLE" in reason.upper():
            lines.append("503 обычно приходит от следующей стороны или из-за недоступного gateway/канала.")

    if context["cdr"]:
        cdr = context["cdr"][0]
        if int(cdr.get("billsec") or 0) > 0:
            lines.append(
                f"Биллинг: последний CDR был с answer, billsec={cdr.get('billsec')}, списано {_money(cdr.get('charged_cents'))}."
            )
        else:
            lines.append("Биллинг: последний CDR без billsec, поэтому баланс не должен списываться.")
    else:
        lines.append("Биллинг: CDR записей пока нет.")

    lines.extend(["", _pcap_summary(events)])
    return "\n".join(lines)


def _openai_analysis(context, question, fallback):
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return fallback
    prompt = {
        "role": "Ты VoIP NOC инженер. Объясняй кратко на русском, кто виноват: клиент, наш switch или терминатор.",
        "question": question,
        "local_analysis": fallback,
        "latest_context": {
            "sip_hits": context["sip_hits"][:10],
            "cdr": context["cdr"][:10],
            "pcap_events": context["pcap_events"][:60],
            "clients": context["clients"][:30],
            "terminators": context["terminators"][:40],
            "client_rates": context["client_rates"][:60],
        },
    }
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "input": [
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False, default=str),
            }
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return fallback + f"\n\nOpenAI не ответил, дал локальный разбор. Ошибка: {_trim(exc, 300)}"

    parts = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                parts.append(content.get("text", ""))
    text = "\n".join(part for part in parts if part).strip()
    return text or fallback


def _help_text(chat_id=None):
    tail = f"\n\nТвой chat_id: {chat_id}" if chat_id else ""
    return (
        "Lexico VoIP bot готов.\n"
        "Команды:\n"
        "/last - разобрать последний хит\n"
        "/hits - последние 5 SIP хитов\n"
        "/cdr - последний CDR\n"
        "/status - краткий статус биллинга\n\n"
        "Можно просто написать вопрос: почему 408, почему не списался баланс, что с последним звонком."
        + tail
    )


def _handle_text(db, chat_id, text):
    context = _latest_context(db)
    cmd = (text or "").strip().lower()
    if cmd in {"/start", "/help"}:
        return _help_text(chat_id)
    if cmd == "/status":
        total_balance = sum(int(client.get("balance_cents") or 0) for client in context["clients"])
        return "\n".join(
            [
                "Статус биллинга:",
                f"Оригинаторов: {len(context['clients'])}",
                f"Групп терминаторов: {len(context['termination_groups'])}",
                f"Терминаторов: {len(context['terminators'])}",
                f"Роутов клиентов: {len(context['client_rates'])}",
                f"Баланс суммарно: {_money(total_balance)}",
                f"SIP хитов видно: {len(context['sip_hits'])}",
                f"CDR видно: {len(context['cdr'])}",
                f"PCAP событий видно: {len(context['pcap_events'])}",
            ]
        )
    if cmd == "/hits":
        return "\n\n".join(_format_hit(hit) for hit in context["sip_hits"][:5])
    if cmd == "/cdr":
        return _latest_cdr_summary(context)
    fallback = _local_analysis(context, text)
    return _openai_analysis(context, text, fallback)


def install(app, main, db):
    @app.post("/telegram/webhook")
    async def telegram_webhook(
        update: TelegramUpdate,
        x_telegram_bot_api_secret_token: Optional[str] = Header(None),
    ):
        expected_secret = _webhook_secret()
        if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad Telegram secret")

        message = update.message or update.edited_message
        if not message and update.callback_query:
            message = update.callback_query.get("message")
        if not message:
            return {"ok": True}

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text") or message.get("caption") or "/last"
        if not chat_id:
            return {"ok": True}

        if not _chat_allowed(chat_id):
            _send_message(
                chat_id,
                "Доступ к биллингу закрыт для этого чата.\n"
                f"Добавь в Railway переменную TELEGRAM_ALLOWED_CHAT_IDS={chat_id}",
            )
            return {"ok": True, "blocked": True}

        try:
            answer = _handle_text(db, chat_id, text)
        except Exception as exc:
            answer = f"Ошибка анализа: {_trim(exc, 800)}"
        _send_message(chat_id, answer)
        return {"ok": True}

    @app.api_route("/api/telegram/set-webhook", methods=["GET", "POST"], dependencies=main.API_AUTH)
    async def telegram_set_webhook(request: Request):
        if not _token():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TELEGRAM_BOT_TOKEN не задан")
        base_url = (os.getenv("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
        webhook_url = f"{base_url}/telegram/webhook"
        payload = {
            "url": webhook_url,
            "allowed_updates": ["message", "edited_message"],
        }
        secret = _webhook_secret()
        if secret:
            payload["secret_token"] = secret
        result = _telegram_api("setWebhook", payload)
        return {"ok": True, "webhook_url": webhook_url, "telegram": result}

    @app.get("/api/telegram/status", dependencies=main.API_AUTH)
    def telegram_status():
        return {
            "ok": True,
            "token_configured": bool(_token()),
            "allowed_chats_configured": bool(_allowed_chat_ids()) or os.getenv("TELEGRAM_ALLOW_ALL") == "1",
            "webhook_secret_configured": bool(_webhook_secret()),
        }
