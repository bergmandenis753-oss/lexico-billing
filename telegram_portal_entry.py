import telegram_standalone as bot


app = bot.app

_base_client_keyboard = bot._client_keyboard
_base_answer_for_callback = bot._answer_for_callback


def _load_client_portal_link(client_id):
    base = bot._billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    return bot._get_json(f"{base}/api/ops/client-portal-link/{client_id}", headers=bot._billing_headers())


def _client_keyboard(client_id):
    keyboard = _base_client_keyboard(client_id)
    rows = keyboard.get("inline_keyboard", [])
    return bot._keyboard([[bot._button("Кабинет", f"client_portal:{client_id}")], *rows])


def _answer_for_callback(data, callback_data):
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


bot._client_keyboard = _client_keyboard
bot._answer_for_callback = _answer_for_callback
