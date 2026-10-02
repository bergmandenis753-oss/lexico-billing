import re
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import telegram_report_files
from telegram_report_files import TextDocument


def _load_client_cdr_duration(bot, client_id, min_billsec):
    base = bot._billing_base_url()
    if not base:
        raise RuntimeError("BILLING_API_BASE_URL не задан")
    query = urllib.parse.urlencode({"min_billsec": int(min_billsec or 0), "limit": 5000})
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
    dial_destination = re.sub(r"\D", "", str(row.get("dial_destination") or ""))
    if dial_destination:
        return dial_destination

    destination = re.sub(r"\D", "", str(row.get("destination") or ""))
    client_prefix = re.sub(r"\D", "", str(row.get("client_tech_prefix") or ""))
    if client_prefix and destination.startswith(client_prefix):
        destination = destination[len(client_prefix):]
    return destination


def _safe_filename_part(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned.strip("._-") or "CDR"


def _report_day(rows):
    for row in rows:
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(row.get("started_at") or ""))
        if match:
            return f"{match.group(3)}-{match.group(2)}"
    return datetime.now(timezone.utc).strftime("%d-%m")


def _format_duration_report(bot, client, report):
    rows = report.get("cdr") or []
    numbers = [number for row in rows if (number := _cdr_number(row))]
    return "\n".join(numbers) + ("\n" if numbers else "")


def _report_document(bot, client, report):
    rows = report.get("cdr") or []
    text = _format_duration_report(bot, client, report)
    if not text:
        return "Нет звонков под этот фильтр."
    client_name = _safe_filename_part(bot._client_name(client))
    return TextDocument(
        f"{client_name}_{_report_day(rows)}.txt",
        text,
        "",
    )


def install(app, bot):
    if getattr(bot, "_cdr_shop_patch_installed", False):
        return
    bot._cdr_shop_patch_installed = True
    telegram_report_files.install(bot)

    pending_by_chat = {}
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
        "/cdrcheck",
        "cdrcheck",
        "/cdr_check",
        "cdr_check",
    }

    def cdr_shop_help(client_id, client_name):
        return (
            f"CDR check для {client_name}.\n"
            "Напиши длительность фильтра следующим сообщением:\n"
            "5\n"
            "05:10\n\n"
            "Или командой:\n"
            f"/cdrshop {client_id} 5\n"
            f"/cdrshop {client_id} 05:10\n\n"
            "5 = минут, 05:10 = минуты:секунды."
        )

    def client_keyboard(client_id):
        keyboard = base_client_keyboard(client_id)
        rows = keyboard.get("inline_keyboard", [])
        if rows:
            rows = [*rows[:1], [bot._button("CDR check", f"client_cdr_shop:{client_id}")], *rows[1:]]
        else:
            rows = [[bot._button("CDR check", f"client_cdr_shop:{client_id}")]]
        return bot._keyboard(
            rows
        )

    def handle_cdr_shop_command(data, text):
        parts = str(text or "").split()
        if len(parts) >= 2 and parts[0].lower().split("@", 1)[0] in {"/cdr", "cdr"} and parts[1].lower() == "check":
            parts = ["/cdrcheck", *parts[2:]]
        if len(parts) < 3:
            return (
                "Формат: /cdrcheck <ID клиента> <минуты или мм:сс>\n"
                "Пример: /cdrcheck 10 5\n"
                "Пример: /cdrcheck 10 05:10",
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
        return _report_document(bot, client, report), client_keyboard(client_id)

    def set_pending(chat_id, callback_data):
        chat_key = str(chat_id)
        callback_text = str(callback_data or "")
        if callback_text.startswith("client_cdr_shop:"):
            pending_by_chat[chat_key] = callback_text.split(":", 1)[1]
            return
        if callback_text in {"menu", "clients"} or callback_text.startswith("client:"):
            pending_by_chat.pop(chat_key, None)

    def answer_pending(data, chat_id, text):
        raw = str(text or "").strip()
        if not raw:
            return None
        first_word = raw.lower().split(maxsplit=1)[0]
        first_word = first_word.split("@", 1)[0]
        lowered = raw.lower()
        if first_word in cdr_shop_commands or lowered.startswith("cdr check ") or lowered.startswith("/cdr check "):
            pending_by_chat.pop(str(chat_id), None)
            return None
        if first_word.startswith("/") or raw.lower() in {"меню", "клиенты", "баланс", "балансы"}:
            pending_by_chat.pop(str(chat_id), None)
            return None

        client_id = pending_by_chat.get(str(chat_id))
        if not client_id:
            return None
        client = bot._client_by_id(data, client_id)
        if not client:
            pending_by_chat.pop(str(chat_id), None)
            return "Клиент не найден.", bot.MAIN_MENU
        try:
            min_billsec = _parse_duration_seconds(raw)
        except ValueError as exc:
            return f"Не понял длительность: {exc}\n\nНапиши, например: 5 или 05:10", client_keyboard(client_id)
        report = _load_client_cdr_duration(bot, client_id, min_billsec)
        return _report_document(bot, client, report), client_keyboard(client_id)

    def answer_for_text(data, text):
        raw = str(text or "").strip()
        cmd = raw.lower()
        first_word = cmd.split(maxsplit=1)[0] if cmd else ""
        first_word = first_word.split("@", 1)[0]
        is_cdr_check_phrase = cmd.startswith("cdr check ") or cmd.startswith("/cdr check ")
        if first_word in cdr_shop_commands or is_cdr_check_phrase:
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

    bot._cdr_shop_set_pending = set_pending
    bot._cdr_shop_answer_pending = answer_pending
    bot._client_keyboard = client_keyboard
    bot._answer_for_callback = answer_for_callback
    bot._answer_for_text = answer_for_text
