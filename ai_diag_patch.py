def apply(ai_diag):
    original_summary = ai_diag._pcap_summary
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

    def inject_dashboard(html):
        html = original_inject(html)
        guard = """
const __lexicoOriginalFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const headers = new Headers(init.headers || {});
  const url = String(input);
  if (url.includes('/api/dashboard-data')) {
    headers.set('X-Money-Scale', String(MONEY_SCALE));
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    const nextInit = {...init, headers, credentials:'same-origin'};
    if (!nextInit.signal) nextInit.signal = controller.signal;
    return __lexicoOriginalFetch(input, nextInit).finally(() => clearTimeout(timeout));
  }
  return __lexicoOriginalFetch(input, {...init, headers, credentials:'same-origin'});
};
"""
        if "__lexicoOriginalFetch" not in html:
            html = html.replace(
                "load();\nsetInterval(() => load(), 5000);",
                guard + "\nload();\nsetInterval(() => load(), 5000);",
            )
        return html

    ai_diag.inject_dashboard = inject_dashboard
