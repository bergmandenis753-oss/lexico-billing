from fastapi import Request


def apply(ai_diag):
    original_summary = ai_diag._pcap_summary
    original_install = ai_diag.install
    original_inject = ai_diag.inject_dashboard

    def pcap_ladder(events, limit=18):
        if not events:
            return ""
        lines = ["Лестница SIP:"]
        for event in events[:limit]:
            if event.get("status_code"):
                status = f"SIP {event.get('status_code')} {event.get('status_text') or ''}".strip()
            else:
                status = event.get("method") or "SIP"
            src = f"{event.get('src_ip') or '?'}:{event.get('src_port') or '?'}"
            dst = f"{event.get('dst_ip') or '?'}:{event.get('dst_port') or '?'}"
            when = str(event.get("observed_at") or "")[-15:]
            lines.append(f"{when} {event.get('direction') or '?'} {src} -> {dst}: {status}")
        if len(events) > limit:
            lines.append(f"...ещё {len(events) - limit} SIP-событий в этом хите.")
        return "\n".join(lines)

    def pcap_summary(events):
        base = original_summary(events)
        ladder = pcap_ladder(events)
        return base + ("\n" + ladder if ladder else "")

    def clean_pcap_event(item):
        data = ai_diag._model_dict(item)
        for key in ai_diag.PCAP_COLUMNS:
            if key == "status_code":
                data[key] = int(data[key]) if data.get(key) is not None else None
            elif key == "raw_summary":
                data[key] = ai_diag._trim(data.get(key), 5000)
            else:
                data[key] = ai_diag._trim(data.get(key), 300)
        return data

    ai_diag._pcap_summary = pcap_summary
    ai_diag._clean_pcap_event = clean_pcap_event

    def rows(items):
        return [dict(row) for row in items]

    def legacy_money(db, value):
        if value is None:
            return value
        return value / db.LEGACY_CENT_TO_MONEY_UNITS

    def legacy_rows(db, items, fields):
        out = []
        for row in items:
            item = dict(row)
            for field in fields:
                if field in item:
                    item[field] = legacy_money(db, item[field])
            out.append(item)
        return out

    def uses_current_money_scale(db, request):
        return request.headers.get("x-money-scale", "").strip() == str(db.MONEY_SCALE)

    def remove_routes(app, path):
        app.router.routes = [route for route in app.router.routes if getattr(route, "path", "") != path]

    def install(app, main, db):
        remove_routes(app, "/api/dashboard-data")

        @app.get("/api/dashboard-data", dependencies=main.ADMIN_AUTH)
        def dashboard_data(request: Request):
            conn = db.get_conn()
            try:
                clients = conn.execute("SELECT * FROM clients ORDER BY id").fetchall()
                groups = conn.execute("SELECT * FROM termination_groups ORDER BY name").fetchall()
                terminators = conn.execute(
                    "SELECT t.*, g.name AS gateway_group_name, g.ips AS gateway_group_ips, "
                    "g.gateway_name AS gateway_group_gateway_name FROM terminators t "
                    "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
                    "ORDER BY t.prefix, t.active DESC, t.id"
                ).fetchall()
                client_rates = conn.execute(
                    "SELECT cr.*, c.name AS client_name, t.name AS terminator_name "
                    "FROM client_rates cr JOIN clients c ON c.id = cr.client_id "
                    "LEFT JOIN terminators t ON t.id = cr.terminator_id ORDER BY cr.client_id"
                ).fetchall()
                cdr = conn.execute(
                    "SELECT cd.*, c.name AS client_name, c.sip_ip AS client_sip_ip, "
                    "c.currency AS client_currency FROM cdr cd LEFT JOIN clients c ON c.id = cd.client_id "
                    "ORDER BY cd.id DESC LIMIT 10"
                ).fetchall()
                sip_hits = conn.execute(
                    "SELECT sh.*, c.currency AS client_currency FROM sip_hits sh "
                    "LEFT JOIN clients c ON c.id = sh.client_id ORDER BY sh.id DESC LIMIT 50"
                ).fetchall()
                try:
                    e164_directions = db.list_e164_countries(conn)
                except Exception:
                    e164_directions = []
                total_balance = conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM clients").fetchone()["s"]
                margin_today = conn.execute(
                    "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE date(started_at)=date('now')"
                ).fetchone()["s"]
                margin_month = conn.execute(
                    "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE strftime('%Y-%m', started_at)=strftime('%Y-%m','now')"
                ).fetchone()["s"]
            finally:
                conn.close()

            if not uses_current_money_scale(db, request):
                return {
                    "money_scale": 100,
                    "clients": legacy_rows(db, clients, ("balance_cents",)),
                    "termination_groups": rows(groups),
                    "terminators": legacy_rows(db, terminators, ("cost_rate_cents",)),
                    "client_rates": legacy_rows(db, client_rates, ("sell_rate_cents",)),
                    "cdr": legacy_rows(db, cdr, ("sell_rate_cents", "cost_rate_cents", "charged_cents", "margin_cents")),
                    "sip_hits": legacy_rows(db, sip_hits, ("sell_rate_cents", "cost_rate_cents")),
                    "e164_directions": e164_directions,
                    "summary": {
                        "total_balance_cents": legacy_money(db, total_balance),
                        "margin_today_cents": legacy_money(db, margin_today),
                        "margin_month_cents": legacy_money(db, margin_month),
                    },
                }

            return {
                "money_scale": db.MONEY_SCALE,
                "clients": rows(clients),
                "termination_groups": rows(groups),
                "terminators": rows(terminators),
                "client_rates": rows(client_rates),
                "cdr": rows(cdr),
                "sip_hits": rows(sip_hits),
                "e164_directions": e164_directions,
                "summary": {
                    "total_balance_cents": total_balance,
                    "margin_today_cents": margin_today,
                    "margin_month_cents": margin_month,
                },
            }

        return original_install(app, main, db)

    def inject_dashboard(html):
        html = original_inject(html)
        guard = """
const __lexicoOriginalFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  const headers = new Headers(init.headers || {});
  if (String(input).includes('/api/dashboard-data')) {
    headers.set('X-Money-Scale', String(MONEY_SCALE));
  }
  return __lexicoOriginalFetch(input, {...init, headers, credentials:'same-origin', signal:init.signal || controller.signal})
    .finally(() => clearTimeout(timeout));
};
"""
        if "__lexicoOriginalFetch" not in html:
            html = html.replace("load();\nsetInterval(() => load(), 5000);", guard + "\nload();\nsetInterval(() => load(), 5000);")
        return html

    ai_diag.install = install
    ai_diag.inject_dashboard = inject_dashboard
