import ipaddress
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

import admin_management_patch
import client_connection_patch


def _remove_routes(app, path, methods=None):
    wanted = {m.upper() for m in methods} if methods else None
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", "") == path
            and (wanted is None or set(getattr(route, "methods", set()) or set()) & wanted)
        )
    ]


def _valid_ip_tokens(raw_ips):
    out = []
    for token in str(raw_ips or "").replace(";", ",").split(","):
        value = token.strip()
        if not value:
            continue
        try:
            ipaddress.ip_address(value)
        except ValueError:
            continue
        out.append(value)
    return out


_CLIENT_ROW_INJECT = r"""<script>
(() => {
  if (window.__clientConnectionRowsV2) return;
  window.__clientConnectionRowsV2 = true;
  const escC = s => String(s ?? '').replace(/[&<>\"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[m]));
  const moneyC = (u, cur) => ((Number(u)||0)/MONEY_SCALE).toFixed(4) + (cur ? ' ' + cur : '');
  const authText = c => {
    const mode = c.auth_mode || 'ip';
    if (mode === 'sip') return `SIP login: ${c.sip_login || ''}`;
    if (mode === 'ip_sip') return `IP+SIP: ${c.sip_ip || ''} / ${c.sip_login || ''}`;
    return c.sip_ip || '';
  };
  async function refreshClientRows() {
    const body = document.getElementById('t-clients');
    if (!body) return;
    const r = await fetch('/api/dashboard-data', {cache:'no-store', headers:{'X-Money-Scale': String(MONEY_SCALE)}});
    if (!r.ok) return;
    const d = await r.json();
    body.innerHTML = (d.clients || []).map(c => {
      const name = window.editManageClientName
        ? `<button class="manage-link-btn" onclick="editManageClientName(${c.id})" title="Переименовать оригинатора">${escC(c.name)}</button>`
        : escC(c.name);
      const del = window.deleteManageClient ? `<button class="small danger" onclick="deleteManageClient(${c.id})">✕</button>` : '';
      return `<tr>
        <td>${name}</td>
        <td class="mut">${escC(authText(c))}</td>
        <td class="right">${moneyC(c.balance_cents, c.currency)}</td>
        <td>${c.active ? '<span class="badge on">активен</span>' : '<span class="badge off">выкл</span>'}</td>
        <td class="right"><div class="manage-actions"><button class="small ghost" onclick="showClientConnection(${c.id})">Данные</button><button class="small" onclick="openTopup(${c.id})">+ Пополнить</button>${del}</div></td>
      </tr>`;
    }).join('') || '<tr><td class="empty" colspan="5">Нет данных</td></tr>';
  }
  if (typeof load === 'function') {
    const previousLoad = load;
    load = async function(manual = false) {
      await previousLoad(manual);
      await refreshClientRows();
    };
  }
  refreshClientRows();
})();
</script>"""


def install(app, main, db):
    _remove_routes(app, "/", {"GET"})
    _remove_routes(app, "/api/firewall-whitelist", {"GET"})

    @app.get("/api/firewall-whitelist", dependencies=main.API_AUTH)
    def firewall_whitelist():
        conn = db.get_conn()
        try:
            client_connection_patch._ensure_columns(conn)
            entries = []
            seen = set()

            def add_entries(raw_ips, **meta):
                for token in _valid_ip_tokens(raw_ips):
                    if token in seen:
                        continue
                    seen.add(token)
                    entries.append({"ip": token, **meta})

            cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
            deleted_filter = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
            for row in conn.execute(
                f"SELECT id, name, sip_ip FROM clients WHERE active = 1 {deleted_filter} ORDER BY id"
            ).fetchall():
                add_entries(row["sip_ip"], source="client", client_id=row["id"], client_name=row["name"])

            for row in conn.execute(
                "SELECT id, name, ips FROM termination_groups WHERE active = 1 ORDER BY id"
            ).fetchall():
                add_entries(row["ips"], source="termination_group", group_id=row["id"], group_name=row["name"])

            for row in conn.execute(
                "SELECT t.id, t.name, t.ips, t.gateway_group_id, g.name AS group_name, g.ips AS group_ips "
                "FROM terminators t LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
                "WHERE t.active = 1 ORDER BY t.id"
            ).fetchall():
                add_entries(row["ips"], source="terminator", terminator_id=row["id"], terminator_name=row["name"])
                add_entries(
                    row["group_ips"],
                    source="terminator_group",
                    terminator_id=row["id"],
                    terminator_name=row["name"],
                    group_id=row["gateway_group_id"],
                    group_name=row["group_name"],
                )
            return {"ok": True, "entries": entries}
        finally:
            conn.close()

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
        html = client_connection_patch._patch_dashboard_html(html)
        html = html.replace("</body>", admin_management_patch._MANAGEMENT_INJECT + "\n" + _CLIENT_ROW_INJECT + "\n</body>")
        response = HTMLResponse(html)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
