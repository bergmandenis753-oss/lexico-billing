"""Bot-only relay: the server polls HTTPS; no public port is opened on the switch."""

import hashlib
import hmac
import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from telegram_report_files import TextDocument


def archive_token(key):
    return hmac.new(key.encode(), b"lexico-telegram-sip-archive-v1", hashlib.sha256).hexdigest()


@dataclass
class ArchiveRequest:
    number: str
    day: str
    data: dict


def request_report(number_text, data):
    from telegram_cdr_check import _parse_number
    raw = number_text.strip()
    day = datetime.now(timezone.utc).date().isoformat()
    match = re.search(r"\s+(\d{4}-\d{2}-\d{2})$", raw)
    if match:
        day = match[1]
        raw = raw[:match.start()]
    if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
        raise ValueError("Дата: ГГГГ-ММ-ДД")
    return ArchiveRequest(_parse_number(raw), day,
                          {key: data.get(key, []) for key in ("clients", "termination_groups", "terminators")})


def _name_for_ip(rows, ip, fallback):
    names = set()
    for row in rows:
        ips = str(row.get("ips") or row.get("ip") or row.get("gateway_group_ips") or row.get("sip_ip") or "")
        if ip in [part.split(":")[0] for part in re.split(r"[,;\s]+", ips)]:
            names.add(str(row.get("name") or row.get("account_name") or fallback))
    return next(iter(names)) if len(names) == 1 else fallback


def format_report(result, data):
    events, channels = result.get("events", []), result.get("channels", [])
    by_uuid = {c["uuid"]: c for c in channels}
    by_sip = {c.get("sip_id"): c for c in channels if c.get("sip_id")}

    def linked(channel):
        if not channel:
            return []
        links = {channel.get(k) for k in ("uuid", "call_uuid", "origin_uuid", "peer_uuid")} - {None, ""}
        return [c for c in channels if links & ({c.get(k) for k in ("uuid", "call_uuid", "origin_uuid", "peer_uuid")} - {None, ""})]

    invites = [e for e in events if e.get("method") == "INVITE"]
    grouped = {}
    for e in invites:
        key = (e["call_id"], e["direction"], e["dst_ip"], e["b"], e["a"], e.get("pai", ""))
        grouped.setdefault(key, e)
    invites = sorted(grouped.values(), key=lambda e: e["at"])
    outgoing = [e for e in invites if e["direction"] == "out"]
    meta = result.get("meta", {})
    lines = [f"Проверка номера: {result['number']} (A или B)", f"Дата: {result['day']} (UTC)",
             f"Исходящих попыток: {len(outgoing)}. SIP INVITE в архиве: {len(invites)}.",
             f"Архив запущен: {meta.get('started_at', 'неизвестно')}",
             "До запуска архива история недоступна. Хранение: 7 дней. Захват: SIP/UDP 5060, 5080.",
             f"Состояние захвата: {meta.get('capture_status', 'неизвестно')}; событий: {meta.get('events_status', 'неизвестно')}."]
    if meta.get("last_gap"):
        lines.append(f"Последний перерыв сбора: {meta['last_gap']}. История может быть неполной.")
    if result.get("limited"):
        lines.append("ВНИМАНИЕ: достигнут защитный лимит 20000 SIP-событий; отчёт неполный.")
    linked_incoming_ids = set()
    for e in outgoing:
        c = by_sip.get(e["call_id"]) or by_uuid.get(e["call_id"])
        inbound = [item for item in linked(c) if item.get("direction") == "inbound"]
        client_ips = {item.get("ip") for item in inbound} - {None, ""}
        client_ip = next(iter(client_ips)) if len(client_ips) == 1 else ""
        linked_incoming_ids.update(item.get("sip_id") for item in inbound)
        client = _name_for_ip(data.get("clients", []), client_ip, "не определён однозначно") if client_ip else "связь с входящим каналом не сохранена"
        provider = _name_for_ip(data.get("termination_groups", []), e["dst_ip"], "")
        provider = provider or _name_for_ip(data.get("terminators", []), e["dst_ip"], e["dst_ip"])
        started = min((item.get("started_at") for item in inbound if item.get("started_at")), default=e['at'])
        inbound_sip_ids = {item.get("sip_id") for item in inbound} - {None, ""}
        incoming_a = {item['a'] for item in invites if item['call_id'] in inbound_sip_ids and item['direction'] == 'in'}
        lines.extend(["", f"Начало: {started} | Клиент: {client}", f"Исходящий INVITE: {e['at']}", f"IP клиента: {client_ip or '-'}",
                      f"A входящий: {', '.join(sorted(incoming_a)) or 'не сохранён'}",
                      f"A отправленный (From): {e.get('a') or 'отсутствует'}", f"PAI: {e.get('pai') or 'отсутствует'}",
                      f"RPID: {e.get('rpid') or 'отсутствует'}", f"B отправленный: {e['b']}",
                      f"Поставщик: {provider} ({e['dst_ip']}:{e['dst_port']})", f"SIP Call-ID: {e['call_id']}"])
        replies = [r for r in events if r["call_id"] == e["call_id"] and r.get("status") and r.get("src_ip") == e["dst_ip"]]
        if replies:
            lines.append("Ответы поставщика: " + "; ".join(f"{r['at']} {r['status']} [{r.get('cseq', '')}] {r.get('reason', '')}" for r in replies))
        else:
            lines.append("Ответ поставщика в архиве не найден.")
        if c:
            seconds = c.get('seconds')
            duration = f"{int(seconds) // 60}:{int(seconds) % 60:02d} ({seconds} сек.)" if str(seconds or '').isdigit() else 'ещё не зафиксирована'
            lines.append(f"Завершение: {c.get('cause') or 'не зафиксировано'}; длительность разговора: {duration}")
        else:
            lines.append("Длительность разговора: не сохранена (нет связанного события завершения).")
    for e in invites:
        if e["direction"] != "in" or e["call_id"] in linked_incoming_ids:
            continue
        client = _name_for_ip(data.get("clients", []), e["src_ip"], e["src_ip"])
        c = by_sip.get(e['call_id']) or by_uuid.get(e['call_id']) or {}
        lines.extend(["", f"Входящая попытка: {c.get('started_at') or e['at']} | {client}", f"A входящий: {e['a']}", f"B входящий: {e['b']}",
                      f"Входящий SIP Call-ID: {e['call_id']}",
                      f"Длительность разговора, сек.: {c.get('seconds') or 'не зафиксирована'}; завершение: {c.get('cause') or 'не зафиксировано'}",
                      "Исходящий A: не подтверждён. Связанного исходящего INVITE в архиве нет."])
        replies = [r for r in events if r["call_id"] == e["call_id"] and r.get("status") and r.get("direction") == "out"]
        lines.extend(f"Ответ клиенту: {r['at']} {r['status']} {r.get('reason', '')}" for r in replies)
    if not invites:
        lines.append("Совпадений в сохранённом архиве нет. Это не доказывает отсутствие звонков до начала сбора или во время перерывов.")
    lines.extend(["", "A/PAI получены из перехваченного исходящего INVITE, не из входящего CLID или настроек подмены.",
                  "Поставщик может изменить дисплей далее. Имена сопоставлены по текущему справочнику IP; неоднозначные совпадения не угадываются."])
    return TextDocument(f"check_{result['number']}_{result['day']}.txt", "\n".join(lines),
                        f"Проверка {result['number']}, {result['day']} UTC. Исходящих попыток: {len(outgoing)}.")


class Relay:
    def __init__(self, bot):
        self.bot = bot
        self.jobs = {}
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=30)
        self.last_poll = 0

    def submit(self, chat_id, request):
        if time.monotonic() - self.last_poll > 90:
            return "Архив SIP сейчас не подключён. Проверка не выполнена; повтори позже."
        with self.lock:
            if len(self.jobs) >= 30:
                return "Очередь проверок занята. Повтори через минуту."
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"chat": chat_id, "request": request, "created": time.monotonic()}
            self.queue.put_nowait({"id": job_id, "number": request.number, "day": request.day})
        timer = threading.Timer(150, self.expire, args=(job_id,))
        timer.daemon = True
        timer.start()
        return f"Проверяю номер {request.number} (A или B) за {request.day} (UTC). Отчёт пришлю файлом."

    def expire(self, job_id):
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if job:
            self.bot._send_message(job["chat"], "Архив не ответил вовремя. Повтори /check; готового отчёта нет.", self.bot.MAIN_MENU)

    def next_job(self):
        self.last_poll = time.monotonic()
        expired = []
        with self.lock:
            for job_id, job in list(self.jobs.items()):
                if time.monotonic() - job["created"] > 150:
                    expired.append(self.jobs.pop(job_id))
        for job in expired:
            self.bot._send_message(job["chat"], "Архив не ответил вовремя. Повтори /check; готового отчёта нет.", self.bot.MAIN_MENU)
        try:
            job = self.queue.get(timeout=20)
            return job if job["id"] in self.jobs else None
        except queue.Empty:
            return None

    def deliver(self, job_id, result):
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if not job:
            return
        request = job["request"]
        if result.get("number") != request.number or result.get("day") != request.day:
            self.bot._send_message(job["chat"], "Архив вернул неподходящий отчёт. Повтори /check.", self.bot.MAIN_MENU)
            return
        if result.get("error"):
            self.bot._send_message(job["chat"], "Не удалось прочитать архив. Повтори /check позже.", self.bot.MAIN_MENU)
            return
        self.bot._send_message(job["chat"], format_report(result, request.data), self.bot.MAIN_MENU)


def install(app, bot):
    relay = Relay(bot)
    original_send = bot._send_message

    def send(chat_id, text, reply_markup=None):
        if isinstance(text, ArchiveRequest):
            text = relay.submit(chat_id, text)
        return original_send(chat_id, text, reply_markup)

    def authorize(request):
        key = bot._billing_key()
        expected = "Bearer " + archive_token(key) if key else ""
        if not expected or not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(403, "Forbidden")

    @app.get("/internal/sip-archive/next", include_in_schema=False)
    def next_job(request: Request):
        authorize(request)
        return {"job": relay.next_job()}

    @app.post("/internal/sip-archive/result/{job_id}", include_in_schema=False)
    async def result(job_id: str, request: Request):
        authorize(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 25_000_000:
                raise HTTPException(413, "Too large")
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid JSON") from None
        await run_in_threadpool(relay.deliver, job_id, data)
        return {"ok": True}

    bot._send_message = send
    bot._archive_request_report = request_report
    bot._sip_archive_relay = relay
