import os
import threading
import time
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import telegram_standalone as bot


app = bot.app

_base_client_keyboard = bot._client_keyboard
_base_answer_for_callback = bot._answer_for_callback
_base_answer_for_text = bot._answer_for_text

bot.MAIN_MENU = bot._keyboard(
    [
        [bot._button("Клиенты", "clients"), bot._button("Балансы", "balance")],
        [bot._button("Низкие балансы", "low_balance"), bot._button("Статус", "status")],
        [bot._button("Последние SIP", "hits"), bot._button("Последние CDR", "cdr")],
        [bot._button("Разбор последнего", "analyze")],
    ]
)


def _load_client_portal_link(client_id):
    base = bot._billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    return bot._get_json(f"{base}/api/ops/client-portal-link/{client_id}", headers=bot._billing_headers())


def _amount_to_minor(value, scale):
    raw = str(value or "").strip().replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Сумма должна быть числом, например 10 или 10.50")
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    return int((amount * int(scale)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _find_client(data, reference):
    ref = str(reference or "").strip().lstrip("#")
    ref_lower = ref.lower()
    for client in data.get("clients", []):
        if str(client.get("id")) == ref:
            return client
    matches = [client for client in data.get("clients", []) if bot._client_name(client).lower() == ref_lower]
    if len(matches) == 1:
        return matches[0]
    return None


def _billing_topup(client_id, amount_cents):
    base = bot._billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    return bot._post_json(
        f"{base}/api/ops/clients/{client_id}/topup",
        {"amount_cents": int(amount_cents), "note": "telegram bot"},
        headers=bot._billing_headers(),
    )


def _low_balance_threshold_cents(data):
    scale = int(data.get("money_scale") or 10000)
    try:
        stored = int(data.get("low_balance_threshold_cents") or 0)
    except (TypeError, ValueError):
        stored = 0
    if stored > 0:
        return stored
    raw = os.getenv("LOW_BALANCE_THRESHOLD_USD", "10").strip().replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        amount = Decimal("10")
    return int((amount * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _set_low_balance_threshold(raw_amount):
    base = bot._billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    return bot._post_json(
        f"{base}/api/ops/low-balance-threshold",
        {"threshold_usd": str(raw_amount or "").strip()},
        headers=bot._billing_headers(),
    )


def _low_balance_threshold_text(data):
    scale = int(data.get("money_scale") or 10000)
    threshold = _low_balance_threshold_cents(data)
    return (
        f"Текущий порог low-balance alert: {bot._money(threshold, scale, 'USD')}\n\n"
        "Изменить:\n"
        "/setthreshold 20\n"
        "Можно дробно: /setthreshold 20.50"
    )


def _handle_low_balance_threshold_command(data, text):
    parts = str(text or "").split(maxsplit=1)
    if len(parts) == 1:
        return _low_balance_threshold_text(data), bot.MAIN_MENU
    scale = int(data.get("money_scale") or 10000)
    result = _set_low_balance_threshold(parts[1])
    threshold = int(result.get("threshold_cents") or _amount_to_minor(parts[1], scale))
    return (
        "Порог low-balance alert изменён.\n"
        f"Новый порог: {bot._money(threshold, scale, result.get('currency') or 'USD')}",
        bot.MAIN_MENU,
    )


_LOW_BALANCE_THRESHOLD_COMMANDS = {
    "/threshold",
    "threshold",
    "/setthreshold",
    "setthreshold",
    "/setlow",
    "setlow",
    "/lowlimit",
    "lowlimit",
    "/alertlimit",
    "alertlimit",
    "/порог",
    "порог",
}


def _low_balance_rows(data):
    threshold = _low_balance_threshold_cents(data)
    rows = []
    for client in data.get("clients", []):
        if not bool(client.get("active", 1)):
            continue
        balance = int(client.get("balance_cents") or 0)
        if balance < threshold:
            rows.append(client)
    return sorted(rows, key=lambda item: int(item.get("balance_cents") or 0))


def _low_balance_text(data):
    scale = int(data.get("money_scale") or 10000)
    threshold = _low_balance_threshold_cents(data)
    rows = _low_balance_rows(data)
    if not rows:
        return f"Низких балансов нет. Порог: {bot._money(threshold, scale, 'USD')}"
    lines = [f"Низкий баланс у оригинаторов. Порог: {bot._money(threshold, scale, 'USD')}"]
    for client in rows:
        cur = client.get("currency") or "USD"
        lines.append(f"{bot._client_name(client)}: {bot._money(client.get('balance_cents'), scale, cur)}")
    return "\n".join(lines)


def _topup_help(client):
    return (
        f"Пополнение клиента {bot._client_name(client)}.\n"
        f"Отправь команду:\n/topup {client.get('id')} 10\n\n"
        "Можно указать дробную сумму: /topup "
        f"{client.get('id')} 10.50"
    )


def _handle_topup_command(data, text):
    parts = str(text or "").split()
    if len(parts) < 3:
        return "Формат: /topup ID сумма\nПример: /topup 12 10", bot.MAIN_MENU
    amount_raw = parts[-1]
    client_ref = " ".join(parts[1:-1])
    client = _find_client(data, client_ref)
    if not client:
        return "Клиент не найден. Открой /clients и возьми ID клиента.", bot.MAIN_MENU
    scale = int(data.get("money_scale") or 10000)
    amount_cents = _amount_to_minor(amount_raw, scale)
    result = _billing_topup(client.get("id"), amount_cents)
    cur = result.get("currency") or client.get("currency") or "USD"
    return (
        "Баланс пополнен.\n"
        f"Клиент: {result.get('client_name') or bot._client_name(client)}\n"
        f"Сумма: {bot._money(result.get('amount_cents'), scale, cur)}\n"
        f"Новый баланс: {bot._money(result.get('balance_cents'), scale, cur)}",
        _client_keyboard(client.get("id")),
    )


def _client_keyboard(client_id):
    keyboard = _base_client_keyboard(client_id)
    rows = keyboard.get("inline_keyboard", [])
    return bot._keyboard(
        [[bot._button("Кабинет", f"client_portal:{client_id}"), bot._button("Пополнить", f"topup:{client_id}")], *rows]
    )


def _answer_for_text(data, text):
    cmd = str(text or "").strip().lower()
    first_word = cmd.split(maxsplit=1)[0] if cmd else ""
    if first_word in _LOW_BALANCE_THRESHOLD_COMMANDS:
        return _handle_low_balance_threshold_command(data, text)
    if cmd.startswith("/topup ") or cmd.startswith("/addbalance ") or cmd.startswith("/пополнить "):
        return _handle_topup_command(data, text)
    if cmd in {"/low", "низкие балансы", "низкий баланс"}:
        return _low_balance_text(data), bot.MAIN_MENU
    return _base_answer_for_text(data, text)


def _answer_for_callback(data, callback_data):
    if callback_data == "low_balance":
        return _low_balance_text(data), bot.MAIN_MENU
    if callback_data.startswith("topup:"):
        client_id = callback_data.split(":", 1)[1]
        client = bot._client_by_id(data, client_id)
        if not client:
            return "Клиент не найден.", bot.MAIN_MENU
        return _topup_help(client), _client_keyboard(client_id)
    if callback_data.startswith("client_portal:"):
        client_id = callback_data.split(":", 1)[1]
        client = bot._client_by_id(data, client_id)
        if not client:
            return "Клиент не найден.", bot.MAIN_MENU
        link = _load_client_portal_link(client_id)
        return (
            f"Личный кабинет клиента {bot._client_name(client)}:\n{link.get('url')}",
            _client_keyboard(client_id),
        )
    return _base_answer_for_callback(data, callback_data)


_alert_lock = threading.Lock()
_alert_started = False
_last_balance_by_client = None
_incident_alert_lock = threading.Lock()
_incident_alert_started = False


_VOICE_NODE_LABELS = {
    "primary": "Основной · 207.154.192.34",
    "secondary": "Новый · 207.154.219.145",
}


def _alert_interval_seconds(name, default_value, minimum):
    try:
        value = int(os.getenv(name, str(default_value)))
    except ValueError:
        value = default_value
    return max(minimum, value)


def _check_low_balance_alerts():
    global _last_balance_by_client
    data = bot._load_diagnostics()
    threshold = _low_balance_threshold_cents(data)
    scale = int(data.get("money_scale") or 10000)
    current = {}
    crossed = []

    for client in data.get("clients", []):
        if not bool(client.get("active", 1)):
            continue
        client_id = str(client.get("id"))
        balance = int(client.get("balance_cents") or 0)
        current[client_id] = balance
        previous = None if _last_balance_by_client is None else _last_balance_by_client.get(client_id)
        if previous is not None and previous >= threshold and balance < threshold:
            crossed.append(client)

    _last_balance_by_client = current
    if not crossed:
        return

    chat_ids = bot._allowed_chat_ids()
    if not chat_ids:
        return

    lines = [f"Баланс упал ниже {bot._money(threshold, scale, 'USD')}:"]
    for client in sorted(crossed, key=lambda item: int(item.get("balance_cents") or 0)):
        cur = client.get("currency") or "USD"
        lines.append(f"{bot._client_name(client)}: {bot._money(client.get('balance_cents'), scale, cur)}")
    message = "\n".join(lines)
    for chat_id in chat_ids:
        bot._send_message(chat_id, message, bot.MAIN_MENU)


def _low_balance_loop():
    time.sleep(10)
    while True:
        try:
            _check_low_balance_alerts()
        except Exception:
            pass
        time.sleep(_alert_interval_seconds("LOW_BALANCE_CHECK_SECONDS", 300, 60))


def _load_pending_system_incidents():
    base = bot._billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    return bot._get_json(
        f"{base}/api/ops/system-incidents/pending",
        headers=bot._billing_headers(),
        timeout=20,
    ).get("incidents", [])


def _mark_system_incident(incident_id, kind):
    base = bot._billing_base_url()
    return bot._post_json(
        f"{base}/api/ops/system-incidents/{incident_id}/notified",
        {"kind": kind},
        headers=bot._billing_headers(),
        timeout=20,
    )


def _download_system_incident_bundle(incident_id):
    base = bot._billing_base_url()
    request = urllib.request.Request(
        f"{base}/api/ops/system-incidents/{incident_id}/bundle",
        headers=bot._billing_headers(),
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _metric(value, suffix, digits=1):
    if value is None:
        return "нет данных"
    return f"{float(value):.{digits}f}{suffix}"


def _system_incident_alert_text(incident):
    node = str(incident.get("voice_node") or "primary")
    server = _VOICE_NODE_LABELS.get(node, node)
    return (
        "ALERT: проблема качества голоса\n"
        f"Сервер: {server}\n"
        f"Время UTC: {incident.get('triggered_at') or 'неизвестно'}\n"
        f"Причина запуска: {incident.get('trigger_reason') or 'порог превышен'}\n"
        f"CPU: {_metric(incident.get('cpu_percent'), '%')} "
        f"(steal {_metric(incident.get('cpu_steal_percent'), '%')})\n"
        f"RTP loss: {_metric(incident.get('packet_loss_percent'), '%', 2)}\n"
        f"Jitter: {_metric(incident.get('jitter_ms'), ' ms')}\n"
        f"Звонков в разговоре: {int(incident.get('active_calls') or 0)}\n"
        f"Предварительный вывод: {incident.get('suspected_cause') or 'пока не определён'}\n\n"
        "Автоматическая запись PCAP и системной диагностики запущена "
        "(до 90 секунд или 8 MB)."
    )


def _check_system_incident_alerts():
    incidents = _load_pending_system_incidents()
    if not incidents:
        return
    chat_ids = bot._allowed_chat_ids()
    if not chat_ids:
        return

    for incident in incidents:
        incident_id = int(incident["id"])
        if not incident.get("alert_sent_at"):
            message = _system_incident_alert_text(incident)
            for chat_id in chat_ids:
                bot._send_message(chat_id, message, bot.MAIN_MENU)
            _mark_system_incident(incident_id, "alert")

        status = incident.get("status")
        if status == "ready" and not incident.get("bundle_sent_at"):
            content = _download_system_incident_bundle(incident_id)
            file_name = incident.get("bundle_name") or f"incident-{incident_id}.tar.gz"
            caption = (
                f"Диагностика #{incident_id} готова. "
                f"Сервер: {_VOICE_NODE_LABELS.get(incident.get('voice_node'), incident.get('voice_node'))}. "
                "В архиве PCAP, нагрузка, UDP-счётчики и состояние FreeSWITCH."
            )
            for chat_id in chat_ids:
                bot._send_document(
                    chat_id,
                    file_name,
                    content,
                    caption=caption,
                    content_type="application/gzip",
                    reply_markup=bot.MAIN_MENU,
                )
            _mark_system_incident(incident_id, "bundle")
        elif status == "failed" and not incident.get("bundle_sent_at"):
            message = (
                f"Диагностика #{incident_id} не записалась. "
                f"Ошибка: {incident.get('error') or 'неизвестная ошибка'}"
            )
            for chat_id in chat_ids:
                bot._send_message(chat_id, message, bot.MAIN_MENU)
            _mark_system_incident(incident_id, "bundle")


def _system_incident_loop():
    time.sleep(5)
    while True:
        try:
            _check_system_incident_alerts()
        except Exception as exc:
            print(f"system incident alert error: {exc}", flush=True)
        time.sleep(_alert_interval_seconds("SYSTEM_INCIDENT_CHECK_SECONDS", 15, 10))


@app.on_event("startup")
def _start_low_balance_monitor():
    global _alert_started
    if os.getenv("LOW_BALANCE_ALERTS_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return
    with _alert_lock:
        if _alert_started:
            return
        _alert_started = True
    thread = threading.Thread(target=_low_balance_loop, daemon=True)
    thread.start()


@app.on_event("startup")
def _start_system_incident_monitor():
    global _incident_alert_started
    if os.getenv("SYSTEM_INCIDENT_ALERTS_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return
    with _incident_alert_lock:
        if _incident_alert_started:
            return
        _incident_alert_started = True
    threading.Thread(target=_system_incident_loop, daemon=True).start()


bot._client_keyboard = _client_keyboard
bot._answer_for_callback = _answer_for_callback
bot._answer_for_text = _answer_for_text

import telegram_client_alerts_patch

telegram_client_alerts_patch.install(app, bot, globals())
