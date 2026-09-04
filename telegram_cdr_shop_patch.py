import urllib.parse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _load_client_cdr_duration(bot, client_id, min_billsec):
    base = bot._billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    query = urllib.parse.urlencode({"min_billsec": int(min_billsec or 0), "limit": 50})
    return bot._get_json(
        f"{base}/api/ops/client-cdr-duration/{client_id}?{query}",
        headers=bot._billing_headers(),
    )


def _parse_duration_seconds(value):
    raw = str(value or "").strip().lower().replace(",", ".")
    if not raw:
        raise ValueError("пустое значение")

    as_seconds = raw.endswith("s") or raw.endswith("sec") or raw.endswith("сек")
    if raw.endswith("sec"):
        raw = raw[:-3].strip()
    elif raw.endswith("сек"):
        raw = raw[:-3].strip()
    elif raw.endswith("s"):
        raw = raw[:-1].strip()

    if ":" in raw:
        parts = raw.split(":")
        if len(parts) not in (2, 3):
            raise ValueError("пример: 05:10 или 1:05:10")
        try:
            nums = [int(part) for part in parts]
        except ValueError as exc:
            raise ValueError("пример: 05:10 или 1:05:10") from exc
        if len(nums) == 2:
            minutes, seconds = nums
            if seconds >= 60:
                raise ValueError("секунды должны быть меньше 60")
            return max(0, minutes * 60 + seconds)
        hours, minutes, seconds = nums
        if minutes >= 60 or seconds >= 60:
            raise ValueError("минуты/секунды должны быть меньше 60")
        return max(0, hours * 3600 + minutes * 60 + seconds)

    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("пример: 5 или 05:10") from exc
    if amount < 0:
        raise ValueError("длительность не может быть отрицательной")
    if as_seconds:
        return int(amount.to_integral_value(rounding=ROUND_HALF_UP))
    return int((amount * Decimal(60)).to_integral_value(rounding=ROUND_HALF_UP))


def _format_duration(seconds):
    total = int(seconds or 0)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _cdr_number(row):
    return row.get("provider_number") or row.get("dial_destination") or row.get("destination") or "-"


def _format_duration_report(bot, client, report):
    scale = int(report.get("money_scale") or 10000)
    rows = report.get("cdr") or []
    threshold = _format_duration(report.get("min_billsec") or 0)
    lines = [
        f"CDR shop: {bot._client_name(client)}",
        f"Сегодня, звонки дольше {threshold}:",
    ]
    if not rows:
        lines.append("Нет звонков под этот фильтр.")
        return "\n".join(lines)

    for row in rows[:50]:
        currency = row.get("client_currency") or report.get("client_currency") or "USD"
        status = row.get("result") or row.get("bridge_hangup_cause") or row.get("hangup_cause") or "-"
        lines.extend(
            [
                "",
                f"{row.get('started_at') or '-'} | {_cdr_number(row)}",
                f"длит. {_format_duration(row.get('billsec'))} | списано {bot._money(row.get('charged_cents'), scale, currency)}",
                f"статус: {status}",
            ]
        )

    text = "\n".join(lines)
    if len(text) > 3900:
        return text[:3900] + "\n...обрезал, слишком длинно"
    return text


def install(app, bot):
    if getattr(bot, "_cdr_shop_patch_installed", False):
        return
    bot._cdr_shop_patch_installed = True

    base_client_keyboard = bot._client_keyboard
    base_answer_for_callback = bot._answer_for_callback
    base_answer_for_text = bot._answer_for_text

    cdr_shop_commands = {
        "/cdrshop",
        "cdrshop",
        "/cdr_shop",
        "cdr_shop",
        "/cdrdur",
        "cdrdur",
    }

    def cdr_shop_help(client_id, client_name):
        return (
            f"CDR shop для {client_name}.\n"
            "Напиши длительность фильтра командой:\n"
            f"/cdrshop {client_id} 5\n"
            f"/cdrshop {client_id} 05:10\n\n"
            "5 = минут, 05:10 = минуты:секунды."
        )

    def client_keyboard(client_id):
        keyboard = base_client_keyboard(client_id)
        rows = keyboard.get("inline_keyboard", [])
        if rows:
            rows = [*rows[:1], [bot._button("CDR shop", f"client_cdr_shop:{client_id}")], *rows[1:]]
        else:
            rows = [[bot._button("CDR shop", f"client_cdr_shop:{client_id}")]]
        return bot._keyboard(
            rows
        )

    def handle_cdr_shop_command(data, text):
        parts = str(text or "").split()
        if len(parts) < 3:
            return (
                "Формат: /cdrshop <ID клиента> <минуты или мм:сс>\n"
                "Пример: /cdrshop 10 5\n"
                "Пример: /cdrshop 10 05:10",
                bot.MAIN_MENU,
            )
        client_id = parts[1]
        client = bot._client_by_id(data, client_id)
        if not client:
            return "Клиент не найден.", bot.MAIN_MENU
        try:
            min_billsec = _parse_duration_seconds(parts[2])
        except ValueError as exc:
            return f"Не понял длительность: {exc}", client_keyboard(client_id)
        report = _load_client_cdr_duration(bot, client_id, min_billsec)
        return _format_duration_report(bot, client, report), client_keyboard(client_id)

    def answer_for_text(data, text):
        raw = str(text or "").strip()
        cmd = raw.lower()
        first_word = cmd.split(maxsplit=1)[0] if cmd else ""
        first_word = first_word.split("@", 1)[0]
        if first_word in cdr_shop_commands:
            return handle_cdr_shop_command(data, raw)
        return base_answer_for_text(data, text)

    def answer_for_callback(data, callback_data):
        if callback_data.startswith("client_cdr_shop:"):
            client_id = callback_data.split(":", 1)[1]
            client = bot._client_by_id(data, client_id)
            if not client:
                return "Клиент не найден.", bot.MAIN_MENU
            return cdr_shop_help(client_id, bot._client_name(client)), client_keyboard(client_id)
        return base_answer_for_callback(data, callback_data)

    bot._client_keyboard = client_keyboard
    bot._answer_for_callback = answer_for_callback
    bot._answer_for_text = answer_for_text
