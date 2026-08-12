import json
import os
import urllib.request

from fastapi import Header, HTTPException, status


def _delete_json(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {}, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def install(app, bot, portal_globals):
    base_answer_for_text = bot._answer_for_text
    base_answer_for_callback = bot._answer_for_callback
    low_balance_threshold_cents = portal_globals["_low_balance_threshold_cents"]

    def find_client(data, reference):
        ref = str(reference or "").strip().lstrip("#")
        ref_lower = ref.lower()
        for client in data.get("clients", []):
            if str(client.get("id")) == ref:
                return client
        matches = [client for client in data.get("clients", []) if bot._client_name(client).lower() == ref_lower]
        return matches[0] if len(matches) == 1 else None

    def client_alert_chat_id(client):
        if not _truthy(client.get("telegram_alerts_enabled")):
            return ""
        return str(client.get("telegram_chat_id") or "").strip()

    def client_for_alert_chat(data, chat_id):
        chat_id = str(chat_id)
        for client in data.get("clients", []):
            if client_alert_chat_id(client) == chat_id:
                return client
        return None

    def billing_base():
        base = bot._billing_base_url()
        if not base:
            raise RuntimeError("BILLING_API_BASE_URL не задан")
        return base

    def set_client_alert_chat(client_id, chat_id, enabled=True):
        return bot._post_json(
            f"{billing_base()}/api/ops/clients/{client_id}/telegram-alert-chat",
            {"chat_id": str(chat_id).strip(), "enabled": bool(enabled)},
            headers=bot._billing_headers(),
        )

    def clear_client_alert_chat(client_id):
        return _delete_json(
            f"{billing_base()}/api/ops/clients/{client_id}/telegram-alert-chat",
            headers=bot._billing_headers(),
        )

    def bind_client_text(data, text):
        parts = str(text or "").split()
        if len(parts) < 3:
            return (
                "Формат:\n"
                "/bindclient <клиент или ID> <chat_id>\n\n"
                "Пример:\n"
                "/bindclient Revo -1001234567890\n\n"
                "В клиентском чате можно написать /chatid, чтобы узнать chat_id."
            ), bot.MAIN_MENU
        chat_id = parts[-1]
        client = find_client(data, " ".join(parts[1:-1]))
        if not client:
            return "Клиент не найден или найдено несколько клиентов.", bot.MAIN_MENU
        result = set_client_alert_chat(client.get("id"), chat_id, True)
        linked = result.get("client") or {}
        return (
            "Telegram-чат привязан к клиенту.\n"
            f"Клиент: {linked.get('name') or bot._client_name(client)}\n"
            f"chat_id: {linked.get('telegram_chat_id') or chat_id}\n"
            "Теперь low-balance alert по этому клиенту будет уходить в этот чат.",
            bot._client_keyboard(client.get("id")),
        )

    def unbind_client_text(data, text):
        parts = str(text or "").split(maxsplit=1)
        if len(parts) < 2:
            return "Формат: /unbindclient <клиент или ID>", bot.MAIN_MENU
        client = find_client(data, parts[1])
        if not client:
            return "Клиент не найден или найдено несколько клиентов.", bot.MAIN_MENU
        clear_client_alert_chat(client.get("id"))
        return f"Telegram-alert чат отвязан от {bot._client_name(client)}.", bot._client_keyboard(client.get("id"))

    def client_chats_text(data):
        rows = []
        for client in data.get("clients", []):
            chat_id = client_alert_chat_id(client)
            if chat_id:
                rows.append(f"{bot._client_name(client)}: {chat_id}")
        if not rows:
            return "Клиентские Telegram-чаты пока не привязаны."
        return "Клиентские Telegram-alert чаты:\n" + "\n".join(rows)

    def answer_for_text(data, text):
        cmd = str(text or "").strip().lower()
        if cmd.startswith("/bindclient ") or cmd.startswith("/bindtg "):
            return bind_client_text(data, text)
        if cmd.startswith("/unbindclient ") or cmd.startswith("/unbindtg "):
            return unbind_client_text(data, text)
        if cmd in {"/clientchats", "/tgchats", "клиентские чаты"}:
            return client_chats_text(data), bot.MAIN_MENU
        return base_answer_for_text(data, text)

    def client_low_balance_alert_text(client, threshold, scale):
        cur = client.get("currency") or "USD"
        return "\n".join(
            [
                "Внимание: низкий баланс.",
                f"Клиент: {bot._client_name(client)}",
                f"Порог: {bot._money(threshold, scale, cur)}",
                f"Текущий баланс: {bot._money(client.get('balance_cents'), scale, cur)}",
                "Пожалуйста, пополните баланс, чтобы звонки не остановились.",
            ]
        )

    def check_low_balance_alerts():
        data = bot._load_diagnostics()
        threshold = low_balance_threshold_cents(data)
        scale = int(data.get("money_scale") or 10000)
        current = {}
        crossed = []

        last_balance = portal_globals.get("_last_balance_by_client")
        for client in data.get("clients", []):
            if not bool(client.get("active", 1)):
                continue
            client_id = str(client.get("id"))
            balance = int(client.get("balance_cents") or 0)
            current[client_id] = balance
            previous = None if last_balance is None else last_balance.get(client_id)
            if previous is not None and previous >= threshold and balance < threshold:
                crossed.append(client)

        portal_globals["_last_balance_by_client"] = current
        if not crossed:
            return

        for client in crossed:
            chat_id = client_alert_chat_id(client)
            if chat_id:
                try:
                    bot._send_message(chat_id, client_low_balance_alert_text(client, threshold, scale))
                except Exception:
                    pass

        if os.getenv("LOW_BALANCE_ADMIN_ALERTS_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
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
            try:
                bot._send_message(chat_id, message, bot.MAIN_MENU)
            except Exception:
                pass

    def remove_webhook_route():
        app.router.routes = [
            route
            for route in app.router.routes
            if not (
                getattr(route, "path", None) == "/telegram/webhook"
                and "POST" in (getattr(route, "methods", set()) or set())
            )
        ]

    def client_chat_help(chat_id, data=None):
        client = client_for_alert_chat(data or {}, chat_id)
        lines = [f"chat_id: {chat_id}"]
        if client:
            lines.append(f"client: {bot._client_name(client)}")
        return "\n".join(lines)

    bot._answer_for_text = answer_for_text
    portal_globals["_check_low_balance_alerts"] = check_low_balance_alerts
    remove_webhook_route()

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        update: bot.TelegramUpdate,
        x_telegram_bot_api_secret_token: str | None = Header(None),
    ):
        secret = bot._webhook_secret()
        if secret and x_telegram_bot_api_secret_token != secret:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad Telegram secret")

        callback = update.callback_query or {}
        message = update.message or update.edited_message or callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return {"ok": True}

        if bot._chat_allowed(chat_id):
            try:
                data = bot._load_diagnostics()
                if callback:
                    bot._answer_callback(callback.get("id"))
                    text, keyboard = base_answer_for_callback(data, callback.get("data") or "menu")
                else:
                    text, keyboard = answer_for_text(data, message.get("text") or "/start")
            except Exception as exc:
                text, keyboard = f"Ошибка бота: {bot._trim(exc, 900)}", bot.MAIN_MENU
            bot._send_message(chat_id, text, keyboard)
            return {"ok": True}

        if callback:
            bot._answer_callback(callback.get("id"))
            return {"ok": True, "blocked": True}

        text = str(message.get("text") or message.get("caption") or "").strip()
        if text.lower() in {"/chatid", "chatid", "/id"}:
            try:
                data = bot._load_diagnostics()
            except Exception:
                data = {}
            bot._send_message(chat_id, client_chat_help(chat_id, data))
        return {"ok": True, "client_chat": True}
