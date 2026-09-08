import re
from datetime import datetime, timezone

from telegram_report_files import TextDocument


def _number(value):
    raw = re.sub(r"[\s()+.-]", "", str(value or ""))
    return raw if re.fullmatch(r"[0-9]{7,24}", raw) else ""


def _parse_number(value):
    number = _number(value)
    if not number:
        raise ValueError("Укажи один B-номер в международном формате, например /check 48506147819")
    return number


def _sip_user(value):
    match = re.search(r"sips?:([^@;>\s]+)", str(value or ""), re.I)
    return match.group(1) if match else ""


def _header_user(event, name):
    match = re.search(rf"(?im)^{re.escape(name)}\s*:\s*([^\r\n]+)", event.get("raw_summary") or "")
    return _sip_user(match.group(1)) if match else ""


def _matches_cdr(row, number):
    return number in {
        _number(row.get(key)) for key in ("destination", "dial_destination", "provider_number")
    }


def _wire_events(data, number, rows):
    # Match exact transmitted destinations, including only known provider prefixes.
    targets = {(str(row.get("route_ip") or "").split(":")[0], _number(row.get("provider_number")))
               for row in rows if row.get("route_ip") and row.get("provider_number")}
    for term in data.get("terminators", []):
        ips = str(term.get("gateway_group_ips") or term.get("ips") or "")
        for ip in re.split(r"[,;\s]+", ips):
            if ip:
                targets.add((ip.split(":")[0], str(term.get("tech_prefix") or "") + number))
    seen = set()
    matched = []
    for event in data.get("pcap_events", []):
        if event.get("direction") != "out" or str(event.get("method") or "").upper() != "INVITE":
            continue
        destination = _number(_sip_user(event.get("request_uri")))
        if not destination or (destination != number and (event.get("dst_ip"), destination) not in targets):
            continue
        key = (event.get("call_id"), event.get("cseq"), event.get("dst_ip"),
               event.get("from_user"), destination, event.get("raw_summary"))
        if key in seen:
            continue
        seen.add(key)
        matched.append(event)
    return sorted(matched, key=lambda event: event.get("observed_at") or "", reverse=True)


def _event_lines(event):
    from_user = _header_user(event, "From") or _header_user(event, "f") or event.get("from_user") or "не сохранён"
    b_number = _sip_user(event.get("request_uri")) or "не сохранён"
    lines = [f"A (исходящий From): {from_user}", f"B (отправленный поставщику): {b_number}"]
    for header, label in (("P-Asserted-Identity", "PAI"), ("Remote-Party-ID", "RPID")):
        value = _header_user(event, header)
        if value:
            lines.append(f"{label}: {value}")
    lines.extend([
        f"Поставщик IP: {event.get('dst_ip') or '-'}:{event.get('dst_port') or '-'}",
        f"SIP Call-ID: {event.get('call_id') or '-'}",
    ])
    return lines


def _report(bot, data, number):
    rows = [row for row in data.get("cdr", []) if _matches_cdr(row, number)]
    events = _wire_events(data, number, rows)
    lines = [
        f"Проверка B: {number}",
        "Поиск по доступным последним CDR и SIP-пакетам (не по всей истории).",
        "A берётся только из исходящего INVITE. Настройки подмены и входящий CLID не являются доказательством отправки.",
        f"CDR найдено: {len(rows)}. Исходящих INVITE: {len(events)}.",
    ]
    used = set()
    for row in rows:
        client = bot._client_by_id(data, row.get("client_id")) or {}
        lines.extend(["", f"CDR #{row.get('id')} | {row.get('started_at') or '-'} UTC",
                      f"Клиент: {row.get('client_name') or bot._client_name(client)}",
                      f"Терминатор: {row.get('terminator_name') or row.get('gateway_name') or '-'}",
                      f"B: {row.get('provider_number') or row.get('dial_destination') or row.get('destination')}"])
        ids = {str(row.get(key)) for key in ("outbound_sip_call_id", "bleg_sip_call_id", "call_uuid") if row.get(key)}
        exact = [event for event in events if event.get("call_id") in ids
                 and (not row.get("route_ip") or event.get("dst_ip") == str(row["route_ip"]).split(":")[0])]
        if exact:
            for event in exact:
                used.add(id(event))
                lines.extend(_event_lines(event))
        else:
            lines.append("A отправленный: не подтверждён. В доступном CDR исходящий Caller ID не сохранён.")
    unlinked = [event for event in events if id(event) not in used]
    if unlinked:
        lines.extend(["", "Исходящие SIP-пакеты на этот B:",
                      "Связь с конкретным CDR/клиентом не подтверждена; ниже фактически отправленные A/B."])
        for event in unlinked:
            lines.extend(["", f"INVITE | {event.get('observed_at') or '-'}"])
            lines.extend(_event_lines(event))
    if not rows and not events:
        lines.append("В доступной выборке совпадений нет. Это не означает, что звонка не было: старые данные API не отдаёт.")
    lines.extend(["", "Отправленный From/PAI не гарантирует такой же дисплей у абонента: его может изменить поставщик."])
    text = "\n".join(lines)
    if len(text) <= 3400:
        return text
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return TextDocument(f"check_{number}_{day}.txt", text,
                        f"Проверка B {number}. CDR: {len(rows)}, исходящих INVITE: {len(events)}.")


def install(bot):
    if getattr(bot, "_cdr_check_installed", False):
        return
    original_text = bot._answer_for_text
    original_pending = bot._cdr_shop_answer_pending
    original_set_pending = bot._cdr_shop_set_pending
    waiting = set()
    prompt = "Пришли B-номер следующим сообщением или командой /check 48506147819"

    def answer_for_text(data, text):
        parts = str(text or "").strip().split(maxsplit=1)
        if not parts or parts[0].lower().split("@", 1)[0] != "/check":
            return original_text(data, text)
        if len(parts) == 1:
            return prompt, bot.MAIN_MENU
        try:
            return _report(bot, data, _parse_number(parts[1])), bot.MAIN_MENU
        except ValueError as exc:
            return str(exc), bot.MAIN_MENU

    def answer_pending(data, chat_id, text):
        key = str(chat_id)
        raw = str(text or "").strip()
        cmd = raw.split(maxsplit=1)[0].lower().split("@", 1)[0] if raw else ""
        if cmd.startswith("/") or raw.lower() in {"меню", "клиенты", "баланс", "балансы"}:
            waiting.discard(key)
            result = original_pending(data, chat_id, text)
            if cmd == "/check" and len(raw.split(maxsplit=1)) == 1:
                waiting.add(key)
                return prompt, bot.MAIN_MENU
            return result
        if key in waiting:
            try:
                number = _parse_number(raw)
            except ValueError as exc:
                return str(exc), bot.MAIN_MENU
            waiting.discard(key)
            return _report(bot, data, number), bot.MAIN_MENU
        return original_pending(data, chat_id, text)

    def set_pending(chat_id, callback_data):
        waiting.discard(str(chat_id))
        return original_set_pending(chat_id, callback_data)

    bot._answer_for_text = answer_for_text
    bot._cdr_shop_answer_pending = answer_pending
    bot._cdr_shop_set_pending = set_pending
    bot._cdr_check_installed = True
