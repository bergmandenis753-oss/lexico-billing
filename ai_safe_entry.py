from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

import ai_diag
import ai_diag_patch
import main_compat


app = main_compat.app
main = main_compat.main
db = main_compat.db

ai_diag_patch.apply(ai_diag)
ai_diag._ensure_pcap_table(db)


@app.on_event("startup")
def _ai_safe_startup():
    ai_diag._ensure_pcap_table(db)


def _remove_route(path, method):
    method = method.upper()
    app.router.routes = [
        route for route in app.router.routes
        if not (getattr(route, "path", "") == path and method in getattr(route, "methods", set()))
    ]


_remove_route("/api/pcap-events", "POST")
_remove_route("/api/ai/analyze", "POST")
_remove_route("/", "GET")


@app.post("/api/pcap-events", dependencies=main.API_AUTH)
def ingest_pcap_events(data: ai_diag.PcapEventsIn):
    events = [ai_diag._clean_pcap_event(event) for event in data.events[:200]]
    return {"ok": True, "saved": ai_diag._record_pcap_events(db, events)}


@app.post("/api/ai/analyze", dependencies=main.ADMIN_AUTH)
def ai_analyze(data: ai_diag.AiAnalyzeIn):
    context = ai_diag._latest_context(db, call_id=data.call_id, limit=data.limit)
    local_text = ai_diag._local_analysis(db, context, data.question)
    ai_text, ai_error = ai_diag._openai_analysis(context, data.question, local_text)
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
    return main_compat._no_store(HTMLResponse(ai_diag.inject_dashboard(html)))
