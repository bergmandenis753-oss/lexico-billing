from pathlib import Path
import json
import math
import os
from typing import Optional
import urllib.error
import urllib.request

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


class PcapEventIn(BaseModel):
    observed_at: str = ""
    direction: str = ""
    src_ip: str = ""
    src_port: str = ""
    dst_ip: str = ""
    dst_port: str = ""
    method: str = ""
    status_code: Optional[int] = None
    status_text: str = ""
    call_id: str = ""
    cseq: str = ""
    from_user: str = ""
    to_user: str = ""
    request_uri: str = ""
    user_agent: str = ""
    reason: str = ""
    raw_summary: str = ""


class PcapEventsIn(BaseModel):
    events: list[PcapEventIn] = Field(default_factory=list)


class AiAnalyzeIn(BaseModel):
    question: str = ""
    call_id: str = ""
    limit: int = Field(default=50, ge=1, le=200)


PCAP_COLUMNS = (
    "observed_at",
    "direction",
    "src_ip",
    "src_port",
    "dst_ip",
    "dst_port",
    "method",
    "status_code",
    "status_text",
    "call_id",
    "cseq",
    "from_user",
    "to_user",
    "request_uri",
    "user_agent",
    "reason",
    "raw_summary",
)


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _trim(value, limit=1000):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit] + "...[cut]"


def _model_dict(item):
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return item.dict()


def _has_route(app, path, method):
    method = method.upper()
    for route in app.router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return True
    return False


def _ensure_pcap_table(db):
    conn = db.get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pcap_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                direction       TEXT    NOT NULL DEFAULT '',
                src_ip          TEXT    NOT NULL DEFAULT '',
                src_port        TEXT    NOT NULL DEFAULT '',
                dst_ip          TEXT    NOT NULL DEFAULT '',
                dst_port        TEXT    NOT NULL DEFAULT '',
                method          TEXT    NOT NULL DEFAULT '',
                status_code     INTEGER,
                status_text     TEXT    NOT NULL DEFAULT '',
                call_id         TEXT    NOT NULL DEFAULT '',
                cseq            TEXT    NOT NULL DEFAULT '',
                from_user       TEXT    NOT NULL DEFAULT '',
                to_user         TEXT    NOT NULL DEFAULT '',
                request_uri     TEXT    NOT NULL DEFAULT '',
                user_agent      TEXT    NOT NULL DEFAULT '',
                reason          TEXT    NOT NULL DEFAULT '',
                raw_summary     TEXT    NOT NULL DEFAULT '',
                created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_pcap_events_created ON pcap_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_pcap_events_call_id ON pcap_events(call_id);
            CREATE INDEX IF NOT EXISTS idx_pcap_events_src_ip ON pcap_events(src_ip);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _record_pcap_events(db, events, keep_latest=3000):
    if not events:
        return 0
    _ensure_pcap_table(db)
    conn = db.get_conn()
    try:
        rows = []
        for event in events:
            row = []
            for col in PCAP_COLUMNS:
                value = event.get(col)
                if value is None and col != "status_code":
                    value = ""
                row.append(value)
            rows.append(row)
        placeholders = ", ".join("?" for _ in PCAP_COLUMNS)
        conn.executemany(
            f"INSERT INTO pcap_events ({', '.join(PCAP_COLUMNS)}) VALUES ({placeholders})",
            rows,
        )
        conn.execute(
            "DELETE FROM pcap_events WHERE id NOT IN "
            "(SELECT id FROM pcap_events ORDER BY id DESC LIMIT ?)",
            (keep_latest,),
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _clean_pcap_event(item):
    data = _model_dict(item)
    for key in PCAP_COLUMNS:
        if key == "status_code":
            data[key] = int(data[key]) if data.get(key) is not None else None
        elif key == "raw_summary":
            data[key] = _trim(data.get(key), 1600)
        else:
            data[key] = _trim(data.get(key), 300)
    return data


def _public_row(row, fields):
    item = dict(row)
    return {field: item.get(field) for field in fields}


def _latest_context(db, call_id="", limit=50):
    _ensure_pcap_table(db)
    conn = db.get_conn()
    try:
        hits = conn.execute(
            "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
            "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cdr = conn.execute(
            "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
            "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
            "ORDER BY cd.id DESC LIMIT 30",
        ).fetchall()
        clients = conn.execute(
            "SELECT id, name, sip_ip, balance_cents, currency, active FROM clients ORDER BY id"
        ).fetchall()
        terminators = conn.execute(
            "SELECT t.id, t.name, t.destination_name, t.prefix, t.tech_prefix, t.cost_rate_cents, "
            "t.active, g.name AS group_name, g.ips AS group_ips, g.gateway_name AS group_gateway_name "
            "FROM terminators t LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
            "ORDER BY t.active DESC, t.prefix, t.id"
        ).fetchall()

        selected_call_id = (call_id or "").strip()
        if not selected_call_id and hits:
            selected_call_id = hits[0]["sip_call_id"] or ""

        if selected_call_id:
            pcap = conn.execute(
                "SELECT * FROM pcap_events WHERE call_id = ? ORDER BY id LIMIT 240",
                (selected_call_id,),
            ).fetchall()
            if len(pcap) < 5:
                more = conn.execute("SELECT * FROM pcap_events ORDER BY id DESC LIMIT 120").fetchall()
                seen = {row["id"] for row in pcap}
                pcap = list(pcap) + [row for row in reversed(more) if row["id"] not in seen]
        else:
            pcap = list(reversed(conn.execute(
                "SELECT * FROM pcap_events ORDER BY id DESC LIMIT 160"
            ).fetchall()))
    finally:
        conn.close()

    return {
        "money_scale": db.MONEY_SCALE,
        "selected_call_id": selected_call_id,
        "sip_hits": [_public_row(row, (
            "id", "created_at", "call_uuid", "sip_ip", "sip_port", "clid", "destination",
            "client_name", "client_tech_prefix", "dial_destination", "provider_number",
            "gateway_name", "route_ip", "terminator_name", "terminator_destination_name",
            "terminator_prefix", "status", "stage", "reason", "max_seconds",
            "sell_rate_cents", "cost_rate_cents", "sip_call_id", "user_agent", "profile",
            "context",
        )) for row in hits],
        "cdr": [_public_row(row, (
            "id", "started_at", "call_uuid", "sip_ip", "clid", "destination",
            "client_name", "client_tech_prefix", "dial_destination", "provider_number",
            "gateway_name", "route_ip", "terminator_name", "terminator_destination_name",
            "terminator_prefix", "terminator_tech_prefix", "hangup_cause",
            "bridge_hangup_cause", "result", "billsec", "sell_rate_cents",
            "cost_rate_cents", "charged_cents", "margin_cents",
        )) for row in cdr],
        "pcap_events": [_public_row(row, (
            "id", "observed_at", "direction", "src_ip", "src_port", "dst_ip", "dst_port",
            "method", "status_code", "status_text", "call_id", "cseq", "from_user",
            "to_user", "request_uri", "user_agent", "reason", "raw_summary",
        )) for row in pcap],
        "clients": [_public_row(row, ("id", "name", "sip_ip", "balance_cents", "currency", "active")) for row in clients],
        "terminators": [_public_row(row, (
            "id", "name", "destination_name", "prefix", "tech_prefix", "cost_rate_cents",
            "active", "group_name", "group_ips", "group_gateway_name",
        )) for row in terminators],
    }


def _money(db, value):
    return f"{(int(value or 0) / db.MONEY_SCALE):.4f} USD"


def _cdr_for_hit(context, hit):
    call_uuid = hit.get("call_uuid") or ""
    if not call_uuid:
        return None
    for row in context["cdr"]:
        if row.get("call_uuid") == call_uuid:
            return row
    return None


def _events_for_hit(context, hit):
    call_id = hit.get("sip_call_id") or context.get("selected_call_id") or ""
    if not call_id:
        return context["pcap_events"]
    matched = [event for event in context["pcap_events"] if event.get("call_id") == call_id]
    return matched or context["pcap_events"]


def _pcap_summary(events):
    if not events:
        return "PCAP-событий по этому звонку пока нет."
    inbound_invites = [
        event for event in events
        if event.get("method") == "INVITE" and event.get("direction") == "in"
    ]
    outbound_invites = [
        event for event in events
        if event.get("method") == "INVITE" and event.get("direction") == "out"
    ]
    statuses = [
        f"{event.get('status_code')} {event.get('status_text') or ''}".strip()
        for event in events if event.get("status_code")
    ]
    methods = {event.get("method") for event in events if event.get("method")}
    parts = [f"PCAP: пакетов {len(events)}"]
    if inbound_invites:
        first = inbound_invites[0]
        parts.append(f"входящий INVITE от {first.get('src_ip')}:{first.get('src_port')}")
    if outbound_invites:
        targets = sorted({f"{event.get('dst_ip')}:{event.get('dst_port')}" for event in outbound_invites})
        parts.append("дальше отправили на " + ", ".join(targets[:4]))
    else:
        parts.append("исходящего INVITE дальше не видно")
    if statuses:
        parts.append("ответы: " + ", ".join(statuses[-10:]))
    if "CANCEL" in methods:
        parts.append("виден CANCEL")
    if "BYE" in methods:
        parts.append("виден BYE")
    return "; ".join(parts) + "."


def _local_analysis(db, context, question):
    hits = context["sip_hits"]
    if not hits:
        return "Пока нет SIP-хитов для разбора. Если звонок точно был, он не дошёл до billing.lua или до SIP-порта сервера."

    hit = hits[0]
    cdr = _cdr_for_hit(context, hit)
    events = _events_for_hit(context, hit)
    lines = [
        f"Последний хит: {hit.get('created_at')} от {hit.get('sip_ip')}:{hit.get('sip_port')}, "
        f"номер {hit.get('destination')}, клиент {hit.get('client_name') or 'не определён'}."
    ]

    status = hit.get("status") or ""
    stage = hit.get("stage") or ""
    reason = hit.get("reason") or ""
    if status in {"blocked", "rejected"}:
        if stage == "client_lookup":
            lines.append(f"Проблема на входе: IP не найден в whitelist. Причина: {reason}")
        elif stage == "client_rate":
            lines.append(f"Проблема в тарифе клиента: не найдено направление/клиентский техпрефикс. Причина: {reason}")
        elif stage == "terminator":
            lines.append(f"Проблема в маршрутизации: нет подходящего терминатора. Причина: {reason}")
        elif stage == "balance":
            lines.append(f"Проблема в балансе/холдах: {reason}")
        else:
            lines.append(f"Звонок отклонён на стадии {stage or 'неизвестно'}: {reason or 'причина не указана'}")
    else:
        lines.append(
            f"Наш сервер звонок принял и выбрал маршрут: {hit.get('terminator_name') or '—'} -> "
            f"{hit.get('gateway_name') or hit.get('route_ip') or '—'}, номер дальше "
            f"{hit.get('provider_number') or hit.get('dial_destination') or '—'}."
        )

    lines.append(_pcap_summary(events))
    status_codes = {event.get("status_code") for event in events if event.get("status_code")}
    has_200 = 200 in status_codes
    has_progress = bool({180, 183} & status_codes)
    has_out_invite = any(event.get("method") == "INVITE" and event.get("direction") == "out" for event in events)
    has_cancel = any(event.get("method") == "CANCEL" for event in events)

    if cdr:
        billsec = int(cdr.get("billsec") or 0)
        if billsec > 0:
            lines.append(
                f"В CDR есть connect: billsec={billsec}, списано {_money(db, cdr.get('charged_cents'))}, "
                f"маржа {_money(db, cdr.get('margin_cents'))}."
            )
        else:
            cause = cdr.get("bridge_hangup_cause") or cdr.get("hangup_cause") or cdr.get("result") or "не указано"
            lines.append(f"В CDR connect не зафиксирован: billsec=0, причина {cause}. Баланс поэтому не списан.")
            if has_progress and not has_200:
                lines.append("Если клиент слышал voicemail/гудки, это похоже на early media: звук был до 200 OK, не настоящий answer.")
            if "RECOVERY_ON_TIMER_EXPIRE" in cause or 408 in status_codes:
                lines.append("Вероятно, таймаут на стороне терминатора или сети до него: исходящий INVITE ушёл, answer не пришёл.")
            if "ORIGINATOR_CANCEL" in cause or has_cancel:
                lines.append("Инициатор отменил звонок до ответа, списания быть не должно.")
    else:
        lines.append("CDR по этому хиту пока не найден. Если звонок уже завершён, надо проверять финализацию billing.lua.")
        if has_200:
            lines.append("В PCAP виден 200 OK, но CDR нет: это подозрение на проблему answer/finalize.")
        elif has_out_invite:
            lines.append("Исходящий INVITE виден, значит наш сервер пытался отправить звонок дальше.")

    lines.append("Call-ID для точного сверения: " + (hit.get("sip_call_id") or context.get("selected_call_id") or "—"))
    if question.strip():
        lines.append("Вопрос: " + question.strip())
    return "\n".join(lines)


def _extract_openai_text(payload):
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    chunks = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def _openai_analysis(context, question, fallback):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY не задан в Railway"

    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        "input": [
            {
                "role": "developer",
                "content": (
                    "Ты read-only помощник Lexico VoIP. Анализируй только переданный JSON: "
                    "sip_hits, cdr, pcap_events, clients, terminators. Ничего не изменяй и не обещай менять. "
                    "Не выдумывай факты. Ответ по-русски: что произошло, дошло ли до нас, отправили ли дальше, "
                    "был ли 200 OK/connect, кто вероятнее отвечает за проблему, что проверить дальше. "
                    "Поля *_cents хранятся в units 0.0001 USD."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question or "Разбери последний звонок.",
                        "local_precheck": fallback,
                        "data": context,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
        "max_output_tokens": 900,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = _extract_openai_text(payload)
        return text or None, "" if text else "OpenAI вернул пустой ответ"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return None, f"OpenAI HTTP {exc.code}: {_trim(detail, 500)}"
    except Exception as exc:
        return None, f"OpenAI недоступен: {exc}"


AI_CSS = """
  .ai-box { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .ai-log { min-height:120px; max-height:360px; overflow:auto; padding:14px; white-space:pre-wrap; }
  .ai-msg { margin:0 0 12px; color:var(--txt); }
  .ai-msg.user { color:#b9d3ff; }
  .ai-msg.muted { color:var(--mut); }
  .ai-form { display:flex; gap:10px; padding:12px; border-top:1px solid var(--line); }
  .ai-form input { flex:1; min-width:0; border:1px solid var(--line); background:var(--bg); color:var(--txt); border-radius:7px; padding:8px; }
  .ai-actions { display:flex; flex-wrap:wrap; gap:8px; padding:0 12px 12px; }
"""

AI_HTML = """
  <section>
    <div class="sec-head"><h2>AI помощник по звонкам</h2><span class="mut" id="ai-state">готов</span></div>
    <div class="ai-box">
      <div class="ai-log" id="ai-log">
        <div class="ai-msg muted">Спроси про последний звонок, 408/503, баланс, whitelist или почему CDR не посчитался.</div>
      </div>
      <div class="ai-actions">
        <button class="small ghost" onclick="askAi('Разбери последний SIP-хит полностью: PCAP, CDR, маршрут, кто виноват и что проверить.')">Последний хит</button>
        <button class="small ghost" onclick="askAi('Почему последний звонок не списал баланс? Был ли настоящий connect/200 OK?')">Баланс</button>
        <button class="small ghost" onclick="askAi('Покажи по последним попыткам: какие IP не в whitelist и какие звонки ушли на терминатора.')">Whitelist</button>
        <button class="small ghost" onclick="askAi('Есть ли в последних звонках early media или voicemail без 200 OK?')">Early media</button>
      </div>
      <form class="ai-form" id="ai-form">
        <input id="ai-question" placeholder="Например: почему последний звонок дал 408?">
        <button type="submit">Разобрать</button>
      </form>
    </div>
  </section>
"""

AI_JS = """
function pushAiMessage(text, kind = '') {
  const log = document.getElementById('ai-log');
  if (!log) return;
  const div = document.createElement('div');
  div.className = `ai-msg ${kind}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function askAi(question) {
  const input = document.getElementById('ai-question');
  const q = String(question || (input ? input.value : '') || '').trim();
  if (!q) return;
  if (input) input.value = '';
  document.getElementById('ai-state').textContent = 'анализирую...';
  pushAiMessage('Ты: ' + q, 'user');
  try {
    const result = await api('/api/ai/analyze', 'POST', {question: q, limit: 50});
    const prefix = result.source === 'openai' ? 'AI' : 'Локальный разбор';
    let text = `${prefix}: ${result.analysis}`;
    if (result.source !== 'openai' && result.ai_error) {
      text += `\n\nOpenAI сейчас не подключён/не ответил: ${result.ai_error}`;
    }
    if (result.pcap_events_count === 0) {
      text += '\n\nPCAP-событий пока нет: проверь, запущен ли сборщик на SIP-сервере.';
    }
    pushAiMessage(text);
    document.getElementById('ai-state').textContent =
      result.source === 'openai' ? 'ответ OpenAI' : 'локальный ответ';
  } catch (err) {
    pushAiMessage('Ошибка анализа: ' + err.message, 'muted');
    document.getElementById('ai-state').textContent = 'ошибка';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('ai-form');
  if (form) {
    form.addEventListener('submit', e => {
      e.preventDefault();
      askAi();
    });
  }
});
"""


def inject_dashboard(html):
    if "AI помощник по звонкам" not in html:
        html = html.replace("</style>", AI_CSS + "\n</style>")
        html = html.replace("</main>", AI_HTML + "\n</main>")
        html = html.replace("</script>", AI_JS + "\n</script>")
    return html


def install(app, main, db):
    _ensure_pcap_table(db)

    @app.on_event("startup")
    def _ai_diag_startup():
        _ensure_pcap_table(db)

    if not _has_route(app, "/api/pcap-events", "POST"):
        @app.post("/api/pcap-events", dependencies=main.API_AUTH)
        def ingest_pcap_events(data: PcapEventsIn):
            events = [_clean_pcap_event(event) for event in data.events[:200]]
            return {"ok": True, "saved": _record_pcap_events(db, events)}

    if not _has_route(app, "/api/ai/analyze", "POST"):
        @app.post("/api/ai/analyze", dependencies=main.ADMIN_AUTH)
        def ai_analyze(data: AiAnalyzeIn):
            context = _latest_context(db, call_id=data.call_id, limit=data.limit)
            local_text = _local_analysis(db, context, data.question)
            ai_text, ai_error = _openai_analysis(context, data.question, local_text)
            latest_hit = context["sip_hits"][0] if context["sip_hits"] else {}
            return {
                "ok": True,
                "source": "openai" if ai_text else "local",
                "analysis": ai_text or local_text,
                "fallback": local_text,
                "ai_error": ai_error,
                "selected_call_id": context.get("selected_call_id") or latest_hit.get("sip_call_id") or "",
                "pcap_events_count": len(context["pcap_events"]),
            }

    app.router.routes = [route for route in app.router.routes if getattr(route, "path", "") != "/"]

    @app.get("/", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def dashboard(request: Request):
        html = Path("dashboard.html").read_text(encoding="utf-8")
        html = html.replace(
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">\n'
            '<meta http-equiv="Pragma" content="no-cache">\n'
            '<meta http-equiv="Expires" content="0">',
        )
        html = html.replace(
            "headers: {'Content-Type':'application/json'}",
            "headers: {'Content-Type':'application/json', 'X-Money-Scale': String(MONEY_SCALE)}",
        )
        html = html.replace(
            "fetch('/api/dashboard-data', {cache:'no-store'})",
            "fetch('/api/dashboard-data', {cache:'no-store', headers: {'X-Money-Scale': String(MONEY_SCALE)}})",
        )
        return _no_store(HTMLResponse(inject_dashboard(html)))
