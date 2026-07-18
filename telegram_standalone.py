import json
import os
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel


app = FastAPI(title="Lexico Telegram diagnostics bot", docs_url=None, redoc_url=None, openapi_url=None)


class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[dict] = None
    edited_message: Optional[dict] = None
    callback_query: Optional[dict] = None


def _trim(value, limit=300):
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _money(value, scale=10000, currency="USD"):
    return f"{(int(value or 0) / scale):.4f} {currency}".strip()


def _token():
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _billing_base_url():
    return (os.getenv("BILLING_API_BASE_URL") or os.getenv("BILLING_BASE_URL") or "").strip().rstrip("/")


def _billing_key():
    return (os.getenv("BILLING_API_SECRET_KEY") or os.getenv("API_SECRET_KEY") or "").strip()


def _webhook_secret():
    return (os.getenv("TELEGRAM_WEBHOOK_SECRET") or os.getenv("BOT_ADMIN_TOKEN") or "").strip()


def _admin_token():
    return (os.getenv("BOT_ADMIN_TOKEN") or os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()


def _allowed_chat_ids():
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def _chat_allowed(chat_id):
    if os.getenv("TELEGRAM_ALLOW_ALL", "").strip().lower() in {"1", "true", "yes"}:
        return True
    allowed = _allowed_chat_ids()
    return bool(allowed) and str(chat_id) in allowed


def _post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _telegram_api(method, payload):
    token = _token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    return _post_json(f"https://api.telegram.org/bot{token}/{method}", payload)


def _send_message(chat_id, text, reply_markup=None):
    text = str(text or "").strip() or "Нет данных."
    chunks = textwrap.wrap(text, width=3500, replace_whitespace=False, drop_whitespace=False)
    for chunk in chunks or [text]:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if reply_markup and chunk == chunks[-1]:
            payload["reply_markup"] = reply_markup
        _telegram_api("sendMessage", payload)


def _answer_callback(callback_id):
    if callback_id:
        try:
            _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception:
            pass


def _keyboard(rows):
    return {"inline_keyboard": rows}


def _button(text, data):
    return {"text": text, "callback_data": data}


MAIN_MENU = _keyboard(
    [
        [_button("Клиенты", "clients"), _button("Балансы", "balance")],
        [_button("Последние SIP", "hits"), _button("Последние CDR", "cdr")],
        [_button("Разбор последнего", "analyze"), _button("Статус", "status")],
    ]
)


def _billing_headers():
    key = _billing_key()
    if not key:
        raise RuntimeError("BILLING_API_SECRET_KEY не задан")
    return {"Authorization": f"Bearer {key}"}


def _load_diagnostics():
    base = _billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    return _get_json(f"{base}/api/ops/diagnostics", headers=_billing_headers())


def _client_name(client):
    return client.get("name") or f"#{client.get('id')}"


def _format_client(client, data):
    scale = int(data.get("money_scale") or 10000)
    cur = client.get("currency") or "USD"
    return "\n".join(
        [
            f"Клиент: {_client_name(client)}",
            f"ID: {client.get('id')}",
            f"IP: {client.get('sip_ip') or '-'}",
            f"Баланс: {_money(client.get('balance_cents'), scale, cur)}",
            f"Статус: {'активен' if client.get('active') else 'выключен'}",
        ]
    )


def _format_hit(hit):
    ip_port = f"{hit.get('sip_ip') or '-'}:{hit.get('sip_port') or '-'}"
    route = hit.get("gateway_name") or hit.get("route_ip") or "-"
    reason = " · ".join(str(x) for x in (hit.get("stage"), hit.get("reason")) if str(x or "").strip())
    return "\n".join(
        [
            f"{hit.get('created_at') or '-'}",
            f"IP: {ip_port}",
            f"CLID: {hit.get('clid') or '-'}",
            f"Номер: {hit.get('destination') or '-'}",
            f"Клиент: {hit.get('client_name') or '-'}",
            f"Дальше: {hit.get('dial_destination') or hit.get('provider_number') or '-'}",
            f"Статус: {hit.get('status') or '-'}",
            f"Причина: {reason or '-'}",
            f"Терминатор: {hit.get('terminator_name') or '-'}",
            f"Куда: {route}",
        ]
    )


def _format_cdr(row, data):
    scale = int(data.get("money_scale") or 10000)
    cur = row.get("client_currency") or "USD"
    return "\n".join(
        [
            f"{row.get('started_at') or '-'}",
            f"Клиент: {row.get('client_name') or row.get('client_id')}",
            f"Вх. IP: {row.get('sip_ip') or row.get('client_sip_ip') or '-'}",
            f"CLID: {row.get('clid') or '-'}",
            f"Номер: {row.get('destination') or '-'}",
            f"Терминатор: {row.get('terminator_name') or row.get('gateway_name') or '-'}",
            f"Billsec: {row.get('billsec')}",
            f"Списано: {_money(row.get('charged_cents'), scale, cur)}",
            f"Маржа: {_money(row.get('margin_cents'), scale, cur)}",
            f"Отбой: {row.get('result') or row.get('bridge_hangup_cause') or row.get('hangup_cause') or '-'}",
        ]
    )


def _client_by_id(data, client_id):
    for client in data.get("clients", []):
        if str(client.get("id")) == str(client_id):
            return client
    return None


def _client_keyboard(client_id):
    return _keyboard(
        [
            [_button("CDR клиента", f"client_cdr:{client_id}"), _button("SIP хиты", f"client_hits:{client_id}")],
            [_button("Назад к клиентам", "clients"), _button("Главное меню", "menu")],
        ]
    )


def _menu_text(data):
    scale = int(data.get("money_scale") or 10000)
    summary = data.get("summary") or {}
    return "\n".join(
        [
            "Lexico VoIP bot",
            f"Оригинаторов: {len(data.get('clients', []))}",
            f"Терминаторов: {len(data.get('terminators', []))}",
            f"SIP хитов: {len(data.get('sip_hits', []))}",
            f"CDR: {len(data.get('cdr', []))}",
            f"Баланс общий: {_money(summary.get('total_balance_cents'), scale, 'USD')}",
        ]
    )


def _clients_menu(data):
    rows = []
    for client in data.get("clients", [])[:40]:
        name = _trim(_client_name(client), 22)
        rows.append([_button(name, f"client:{client.get('id')}")])
    rows.append([_button("Главное меню", "menu")])
    return "Выбери клиента:", _keyboard(rows)


def _balances_text(data):
    scale = int(data.get("money_scale") or 10000)
    lines = ["Балансы клиентов:"]
    for client in data.get("clients", []):
        lines.append(
            f"{_client_name(client)}: {_money(client.get('balance_cents'), scale, client.get('currency') or 'USD')}"
        )
    return "\n".join(lines)


def _hits_text(data, client_id=None):
    hits = data.get("sip_hits", [])
    if client_id is not None:
        hits = [hit for hit in hits if str(hit.get("client_id")) == str(client_id)]
    if not hits:
        return "SIP хитов не найдено."
    return "\n\n".join(_format_hit(hit) for hit in hits[:8])


def _cdr_text(data, client_id=None):
    rows = data.get("cdr", [])
    if client_id is not None:
        rows = [row for row in rows if str(row.get("client_id")) == str(client_id)]
    if not rows:
        return "CDR не найден."
    return "\n\n".join(_format_cdr(row, data) for row in rows[:8])


def _pcap_for_hit(data, hit):
    call_ids = {str(hit.get("sip_call_id") or ""), str(hit.get("call_uuid") or "")}
    call_ids.discard("")
    events = data.get("pcap_events", [])
    if call_ids:
        matched = [event for event in events if str(event.get("call_id") or "") in call_ids]
        if matched:
            return matched[:20]
    ip = str(hit.get("sip_ip") or "")
    return [event for event in events if event.get("src_ip") == ip or event.get("dst_ip") == ip][:12]


def _local_analysis(data):
    hits = data.get("sip_hits", [])
    if not hits:
        return "Последних SIP хитов нет."
    hit = hits[0]
    lines = ["Разбор последнего SIP хита:", _format_hit(hit), ""]
    stage = str(hit.get("stage") or "")
    status_value = str(hit.get("status") or "")
    reason = str(hit.get("reason") or "")
    if status_value == "rejected":
        if stage == "client_lookup":
            lines.append("Вывод: IP отправителя не найден в активных оригинаторах.")
        elif stage == "client_rate":
            lines.append("Вывод: клиент найден, но нет продажного роута/тарифа под этот номер или техпрефикс.")
        elif stage == "terminator":
            lines.append("Вывод: продажный роут есть, но активный терминатор для направления не найден.")
        elif stage == "balance":
            lines.append("Вывод: недостаточно свободного баланса или баланс занят холдами.")
        else:
            lines.append(f"Вывод: отказ на нашем switch. Причина: {reason or stage or '-'}")
    else:
        lines.append("Вывод: наш switch принял звонок и попробовал отправить его дальше.")
        if "408" in reason:
            lines.append("408: дальше по маршруту не пришёл ответ вовремя.")
        if "503" in reason or "SERVICE_UNAVAILABLE" in reason.upper():
            lines.append("503: дальше недоступен gateway/канал или отказал терминатор.")

    cdr_rows = data.get("cdr", [])
    if cdr_rows:
        cdr = cdr_rows[0]
        billsec = int(cdr.get("billsec") or 0)
        if billsec > 0:
            lines.append(f"Последний CDR с answer: billsec={billsec}, списано {_money(cdr.get('charged_cents'), int(data.get('money_scale') or 10000), cdr.get('client_currency') or 'USD')}.")
        else:
            lines.append("Последний CDR без billsec: баланс списываться не должен.")
    else:
        lines.append("CDR записей пока нет.")

    pcap = _pcap_for_hit(data, hit)
    if pcap:
        lines.append("")
        lines.append("SIP пакеты рядом с хитом:")
        for event in pcap[:10]:
            code = f"SIP {event.get('status_code')} {event.get('status_text') or ''}".strip() if event.get("status_code") else event.get("method")
            lines.append(
                f"{event.get('observed_at') or event.get('created_at') or '-'} | "
                f"{event.get('src_ip')}:{event.get('src_port')} -> {event.get('dst_ip')}:{event.get('dst_port')} | {code}"
            )
    return "\n".join(lines)


def _openai_analysis(data, question, fallback):
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return fallback
    prompt = {
        "role": "Ты VoIP NOC инженер. Объясняй кратко на русском, кто виноват: клиент, наш switch или терминатор.",
        "question": question,
        "local_analysis": fallback,
        "context": {
            "sip_hits": data.get("sip_hits", [])[:10],
            "cdr": data.get("cdr", [])[:10],
            "pcap_events": data.get("pcap_events", [])[:60],
            "clients": data.get("clients", [])[:40],
            "terminators": data.get("terminators", [])[:60],
            "client_rates": data.get("client_rates", [])[:80],
        },
    }
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "input": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)}],
    }
    try:
        payload = _post_json(
            "https://api.openai.com/v1/responses",
            body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=35,
        )
    except Exception as exc:
        return fallback + f"\n\nOpenAI не ответил, показал локальный разбор: {_trim(exc, 250)}"
    parts = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                parts.append(content.get("text", ""))
    return "\n".join(part for part in parts if part).strip() or fallback


def _answer_for_text(data, text):
    text = (text or "").strip()
    cmd = text.lower()
    if cmd in {"/start", "/help", "меню"}:
        return _menu_text(data), MAIN_MENU
    if cmd in {"/clients", "клиенты"}:
        return _clients_menu(data)
    if cmd in {"/balance", "балансы", "баланс"}:
        return _balances_text(data), MAIN_MENU
    if cmd in {"/hits", "последние sip"}:
        return _hits_text(data), MAIN_MENU
    if cmd in {"/cdr", "последние cdr"}:
        return _cdr_text(data), MAIN_MENU
    fallback = _local_analysis(data)
    return _openai_analysis(data, text, fallback), MAIN_MENU


def _answer_for_callback(data, callback_data):
    if callback_data == "menu":
        return _menu_text(data), MAIN_MENU
    if callback_data == "clients":
        return _clients_menu(data)
    if callback_data == "balance":
        return _balances_text(data), MAIN_MENU
    if callback_data == "hits":
        return _hits_text(data), MAIN_MENU
    if callback_data == "cdr":
        return _cdr_text(data), MAIN_MENU
    if callback_data == "status":
        return _menu_text(data), MAIN_MENU
    if callback_data == "analyze":
        fallback = _local_analysis(data)
        return _openai_analysis(data, "Разбери последний звонок", fallback), MAIN_MENU
    if callback_data.startswith("client:"):
        client_id = callback_data.split(":", 1)[1]
        client = _client_by_id(data, client_id)
        if not client:
            return "Клиент не найден.", MAIN_MENU
        return _format_client(client, data), _client_keyboard(client_id)
    if callback_data.startswith("client_cdr:"):
        client_id = callback_data.split(":", 1)[1]
        return _cdr_text(data, client_id), _client_keyboard(client_id)
    if callback_data.startswith("client_hits:"):
        client_id = callback_data.split(":", 1)[1]
        return _hits_text(data, client_id), _client_keyboard(client_id)
    return "Не понял команду.", MAIN_MENU


def _require_admin(authorization: Optional[str]):
    expected = _admin_token()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "BOT_ADMIN_TOKEN не задан")
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad admin token")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(
    update: TelegramUpdate,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    secret = _webhook_secret()
    if secret and x_telegram_bot_api_secret_token != secret:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad Telegram secret")

    callback = update.callback_query or {}
    message = update.message or update.edited_message or callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    if not _chat_allowed(chat_id):
        _send_message(
            chat_id,
            "Доступ закрыт.\n"
            f"Добавь в Railway переменную TELEGRAM_ALLOWED_CHAT_IDS={chat_id}",
        )
        _answer_callback(callback.get("id"))
        return {"ok": True, "blocked": True}

    try:
        data = _load_diagnostics()
        if callback:
            _answer_callback(callback.get("id"))
            text, keyboard = _answer_for_callback(data, callback.get("data") or "menu")
        else:
            text, keyboard = _answer_for_text(data, message.get("text") or "/start")
    except Exception as exc:
        text, keyboard = f"Ошибка бота: {_trim(exc, 900)}", MAIN_MENU
    _send_message(chat_id, text, keyboard)
    return {"ok": True}


@app.api_route("/api/telegram/set-webhook", methods=["GET", "POST"])
async def telegram_set_webhook(request: Request, authorization: Optional[str] = Header(None)):
    _require_admin(authorization)
    if not _token():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TELEGRAM_BOT_TOKEN не задан")
    base_url = (os.getenv("PUBLIC_BOT_BASE_URL") or str(request.base_url)).rstrip("/")
    webhook_url = f"{base_url}/telegram/webhook"
    payload = {"url": webhook_url, "allowed_updates": ["message", "edited_message", "callback_query"]}
    secret = _webhook_secret()
    if secret:
        payload["secret_token"] = secret
    result = _telegram_api("setWebhook", payload)
    return {"ok": True, "webhook_url": webhook_url, "telegram": result}


@app.get("/api/telegram/status")
def telegram_status(authorization: Optional[str] = Header(None)):
    _require_admin(authorization)
    return {
        "ok": True,
        "telegram_token_configured": bool(_token()),
        "billing_base_url_configured": bool(_billing_base_url()),
        "billing_key_configured": bool(_billing_key()),
        "allowed_chats_configured": bool(_allowed_chat_ids()) or os.getenv("TELEGRAM_ALLOW_ALL") == "1",
        "webhook_secret_configured": bool(_webhook_secret()),
    }
