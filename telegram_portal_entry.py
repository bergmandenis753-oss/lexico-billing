import os
import threading
import time
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
    raw = os.getenv("LOW_BALANCE_THRESHOLD_USD", "10").strip().replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        amount = Decimal("10")
    return int((amount * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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


bot._client_keyboard = _client_keyboard
bot._answer_for_callback = _answer_for_callback
bot._answer_for_text = _answer_for_text
