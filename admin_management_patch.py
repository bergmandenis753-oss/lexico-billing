from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


class ClientRatePatchIn(BaseModel):
    terminator_id: Optional[int] = None
    client_tech_prefix: Optional[str] = None
    prefix: Optional[str] = None
    destination_name: Optional[str] = None
    sell_rate_cents: Optional[int] = None


class TerminationGroupPatchIn(BaseModel):
    name: Optional[str] = None
    ips: Optional[str] = None
    gateway_name: Optional[str] = None
    active: Optional[bool] = None


def _model_fields(data):
    if hasattr(data, "model_dump"):
        return {k: v for k, v in data.model_dump().items() if v is not None}
    return {k: v for k, v in data.dict().items() if v is not None}


def _rows(rows):
    return [dict(row) for row in rows]


MANUAL_MARGIN_ADJUSTMENT = 663900
MANUAL_MARGIN_DAY = "2026-07-24"
MANUAL_MARGIN_MONTH = "2026-07"


def _apply_manual_margin_adjustment(conn, margin_today, margin_month):
    today = conn.execute("SELECT date('now') AS d").fetchone()["d"]
    month = conn.execute("SELECT strftime('%Y-%m','now') AS m").fetchone()["m"]
    if today == MANUAL_MARGIN_DAY:
        margin_today -= MANUAL_MARGIN_ADJUSTMENT
    if month == MANUAL_MARGIN_MONTH:
        margin_month -= MANUAL_MARGIN_ADJUSTMENT
    return margin_today, margin_month


def _remove_routes(app, path, methods=None):
    wanted = {m.upper() for m in methods} if methods else None
    kept = []
    for route in app.router.routes:
        route_path = getattr(route, "path", "")
        route_methods = set(getattr(route, "methods", set()) or set())
        if route_path == path and (wanted is None or route_methods & wanted):
            continue
        kept.append(route)
    app.router.routes = kept


def install(app, main, db, compat):
    def _no_store(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    def _uses_current_money_scale(request: Request) -> bool:
        return request.headers.get("x-money-scale", "").strip() == str(db.MONEY_SCALE)

    def _legacy_money(value):
        if value is None:
            return value
        return value / db.LEGACY_CENT_TO_MONEY_UNITS

    def _legacy_rows(rows, fields):
        out = []
        for row in rows:
            item = dict(row)
            for field in fields:
                if field in item:
                    item[field] = _legacy_money(item[field])
            out.append(item)
        return out

    def _ensure_client_deleted_at(conn):
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()]
        if "deleted_at" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN deleted_at TEXT")
            conn.commit()

    _remove_routes(app, "/api/dashboard-data", {"GET"})
    _remove_routes(app, "/", {"GET"})
    _remove_routes(app, "/api/firewall-whitelist", {"GET"})
    _remove_routes(app, "/api/client-rates/{rid}", {"PATCH"})
    _remove_routes(app, "/api/clients", {"GET"})

    @app.get("/api/firewall-whitelist", dependencies=main.API_AUTH)
    def firewall_whitelist():
        conn = db.get_conn()
        try:
            _ensure_client_deleted_at(conn)
            entries = []
            seen = set()

            def add_entries(raw_ips, **meta):
                for token in db.split_ip_list(raw_ips):
                    if token in seen:
                        continue
                    seen.add(token)
                    entries.append({"ip": token, **meta})

            clients = conn.execute(
                "SELECT id, name, sip_ip FROM clients WHERE active = 1 AND deleted_at IS NULL ORDER BY id"
            ).fetchall()
            for row in clients:
                add_entries(row["sip_ip"], source="client", client_id=row["id"], client_name=row["name"])

            groups = conn.execute(
                "SELECT id, name, ips FROM termination_groups WHERE active = 1 ORDER BY id"
            ).fetchall()
            for row in groups:
                add_entries(row["ips"], source="termination_group", group_id=row["id"], group_name=row["name"])

            terminators = conn.execute(
                "SELECT t.id, t.name, t.ips, t.gateway_group_id, g.name AS group_name, g.ips AS group_ips "
                "FROM terminators t "
                "LEFT JOIN termination_groups g ON g.id = t.gateway_group_id "
                "WHERE t.active = 1 ORDER BY t.id"
            ).fetchall()
            for row in terminators:
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

    @app.get("/api/dashboard-data", dependencies=main.ADMIN_AUTH)
    def dashboard_data(request: Request):
        conn = db.get_conn()
        try:
            _ensure_client_deleted_at(conn)
            clients = conn.execute("SELECT * FROM clients WHERE deleted_at IS NULL ORDER BY id").fetchall()
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
                "LEFT JOIN terminators t ON t.id = cr.terminator_id "
                "WHERE c.deleted_at IS NULL ORDER BY cr.client_id"
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
            e164_directions = db.list_e164_countries(conn)
            total_balance = conn.execute(
                "SELECT COALESCE(SUM(balance_cents),0) AS s FROM clients WHERE deleted_at IS NULL"
            ).fetchone()["s"]
            margin_today = conn.execute(
                "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE date(started_at)=date('now')"
            ).fetchone()["s"]
            margin_month = conn.execute(
                "SELECT COALESCE(SUM(margin_cents),0) AS s FROM cdr WHERE strftime('%Y-%m', started_at)=strftime('%Y-%m','now')"
            ).fetchone()["s"]
            margin_today, margin_month = _apply_manual_margin_adjustment(conn, margin_today, margin_month)
        finally:
            conn.close()

        if not _uses_current_money_scale(request):
            return {
                "money_scale": 100,
                "clients": _legacy_rows(clients, ("balance_cents",)),
                "termination_groups": _rows(groups),
                "terminators": _legacy_rows(terminators, ("cost_rate_cents",)),
                "client_rates": _legacy_rows(client_rates, ("sell_rate_cents",)),
                "cdr": _legacy_rows(cdr, ("sell_rate_cents", "cost_rate_cents", "charged_cents", "margin_cents")),
                "sip_hits": _legacy_rows(sip_hits, ("sell_rate_cents", "cost_rate_cents")),
                "e164_directions": e164_directions,
                "summary": {
                    "total_balance_cents": _legacy_money(total_balance),
                    "margin_today_cents": _legacy_money(margin_today),
                    "margin_month_cents": _legacy_money(margin_month),
                },
            }

        return {
            "money_scale": db.MONEY_SCALE,
            "clients": _rows(clients),
            "termination_groups": _rows(groups),
            "terminators": _rows(terminators),
            "client_rates": _rows(client_rates),
            "cdr": _rows(cdr),
            "sip_hits": _rows(sip_hits),
            "e164_directions": e164_directions,
            "summary": {
                "total_balance_cents": total_balance,
                "margin_today_cents": margin_today,
                "margin_month_cents": margin_month,
            },
        }

    @app.get("/api/clients", dependencies=main.ADMIN_AUTH)
    def list_clients():
        conn = db.get_conn()
        try:
            _ensure_client_deleted_at(conn)
            rows = conn.execute("SELECT * FROM clients WHERE deleted_at IS NULL ORDER BY id").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    @app.delete("/api/clients/{cid}", dependencies=main.ADMIN_AUTH)
    def delete_client(cid: int):
        conn = db.get_conn()
        try:
            _ensure_client_deleted_at(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL", (cid,)).fetchone()
            if row is None:
                conn.rollback()
                raise HTTPException(404, "Оригинатор не найден")
            conn.execute("DELETE FROM client_rates WHERE client_id = ?", (cid,))
            conn.execute("DELETE FROM reservations WHERE client_id = ?", (cid,))
            archived_ip = f"deleted:{cid}:{db.now()}:{row['sip_ip']}"
            conn.execute(
                "UPDATE clients SET active = 0, sip_ip = ?, deleted_at = datetime('now') WHERE id = ?",
                (archived_ip, cid),
            )
            conn.commit()
            return {"ok": True}
        except HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.patch("/api/termination-groups/{gid}", dependencies=main.ADMIN_AUTH)
    def update_termination_group(gid: int, data: TerminationGroupPatchIn):
        fields = _model_fields(data)
        if not fields:
            raise HTTPException(400, "Нет полей для обновления")
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT * FROM termination_groups WHERE id = ?", (gid,)).fetchone()
            if row is None:
                raise HTTPException(404, "Группа не найдена")
            next_ips = fields.get("ips", row["ips"]) or ""
            next_gateway = fields.get("gateway_name", row["gateway_name"]) or ""
            if not next_gateway.strip() and (not db.split_ip_list(next_ips)):
                raise HTTPException(400, "Укажите IP группы или FreeSWITCH gateway")
            if "active" in fields:
                fields["active"] = int(fields["active"])
            if "gateway_name" in fields:
                fields["gateway_name"] = (fields["gateway_name"] or "").strip()
            sets = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(f"UPDATE termination_groups SET {sets} WHERE id = ?", (*fields.values(), gid))
            conn.commit()
            return {"ok": True}
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, "Группа с таким именем уже существует")
        finally:
            conn.close()

    @app.patch("/api/client-rates/{rid}", dependencies=main.ADMIN_AUTH)
    def update_client_rate(rid: int, data: ClientRatePatchIn):
        fields = _model_fields(data)
        if not fields:
            raise HTTPException(400, "Нет полей для обновления")
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT * FROM client_rates WHERE id = ?", (rid,)).fetchone()
            if row is None:
                raise HTTPException(404, "Тариф не найден")
            if "terminator_id" in fields:
                term = db.get_terminator(conn, fields["terminator_id"])
                if term is None:
                    raise HTTPException(404, "Терминатор не найден")
                next_prefix = fields.get("prefix", row["prefix"])
                next_destination = fields.get("destination_name", row["destination_name"])
                same_prefix = str(term["prefix"]) == str(next_prefix)
                same_direction = db.direction_matches(term["destination_name"], next_destination)
                if not (same_prefix or same_direction):
                    raise HTTPException(400, "Этот терминатор не похож на то же направление/префикс")
            sets = ", ".join(f"{key} = ?" for key in fields)
            cur = conn.execute(f"UPDATE client_rates SET {sets} WHERE id = ?", (*fields.values(), rid))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(404, "Тариф не найден")
            return {"ok": True}
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
        html = html.replace("</body>", _MANAGEMENT_INJECT + "\n</body>")
        return _no_store(HTMLResponse(html))


_MANAGEMENT_INJECT = """<style>
.manage-link-btn { background: transparent; border: 0; color: var(--txt); padding: 0; font: inherit; font-weight: 600; text-align: left; }
.manage-link-btn:hover { color: #b9d3ff; opacity: 1; }
.manage-actions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; }
</style>
<dialog id="manage-rate-term-dlg">
  <h3>Сменить терминатора</h3>
  <form id="manage-rate-term-form">
    <label>Терминатор для этого направления</label>
    <select id="manage-rate-term-select" required></select>
    <div class="mut" id="manage-rate-term-hint" style="margin:-4px 0 14px"></div>
    <div class="row">
      <button type="button" class="ghost" onclick="document.getElementById('manage-rate-term-dlg').close()">Отмена</button>
      <button type="submit">Сохранить</button>
    </div>
  </form>
</dialog>
<script>
(() => {
  const state = {clients: {}, groups: {}, terms: [], rates: {}};
  let editingRateId = null;
  const headers = () => ({'Content-Type':'application/json', 'X-Money-Scale': String(MONEY_SCALE)});
  const money4 = (u, cur) => ((Number(u)||0)/MONEY_SCALE).toFixed(4) + (cur ? ' ' + cur : '');
  const esc2 = s => String(s ?? '').replace(/[&<>\"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[m]));
  const termLabel = t => `${t.name} · ${t.destination_name} (${t.prefix})${t.active ? ' · активен' : ' · резерв'}`;

  async function manageApi(path, method, body) {
    const r = await fetch(path, {method, headers: headers(), body: body ? JSON.stringify(body) : undefined});
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  async function fetchManageData() {
    const r = await fetch('/api/dashboard-data', {cache: 'no-store', headers: {'X-Money-Scale': String(MONEY_SCALE)}});
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  function remember(d) {
    state.clients = {};
    (d.clients || []).forEach(c => state.clients[c.id] = c);
    state.groups = {};
    (d.termination_groups || []).forEach(g => state.groups[g.id] = g);
    state.terms = d.terminators || [];
    state.rates = {};
    (d.client_rates || []).forEach(r => state.rates[r.id] = r);
  }

  function renderManageTables(d) {
    remember(d);
    const clientsBody = document.getElementById('t-clients');
    if (clientsBody) {
      clientsBody.innerHTML = (d.clients || []).map(c => `
        <tr>
          <td><button class="manage-link-btn" onclick="editManageClientName(${c.id})" title="Переименовать оригинатора">${esc2(c.name)}</button></td>
          <td class="mut">${esc2(c.sip_ip)}</td>
          <td class="right">${money4(c.balance_cents, c.currency)}</td>
          <td>${c.active ? '<span class="badge on">активен</span>' : '<span class="badge off">выкл</span>'}</td>
          <td class="right"><div class="manage-actions"><button class="small" onclick="openTopup(${c.id})">+ Пополнить</button><button class="small danger" onclick="deleteManageClient(${c.id})">✕</button></div></td>
        </tr>`).join('') || '<tr><td class="empty" colspan="5">Нет данных</td></tr>';
    }

    const groupsBody = document.getElementById('t-groups');
    if (groupsBody) {
      groupsBody.innerHTML = (d.termination_groups || []).map(g => `
        <tr>
          <td><button class="manage-link-btn" onclick="editManageGroupName(${g.id})" title="Переименовать группу">${esc2(g.name)}</button></td>
          <td class="mut">${esc2(g.ips || '')}</td>
          <td>${g.gateway_name ? esc2(g.gateway_name) : '<span class="mut">direct IP</span>'}</td>
          <td>${g.active ? '<span class="badge on">активна</span>' : '<span class="badge off">выкл</span>'}</td>
          <td class="right"><button class="small danger" onclick="delGroup(${g.id})">✕</button></td>
        </tr>`).join('') || '<tr><td class="empty" colspan="5">Нет данных</td></tr>';
    }

    const ratesBody = document.getElementById('t-rates');
    if (ratesBody) {
      ratesBody.innerHTML = (d.client_rates || []).map(r => {
        const cur = (state.clients[r.client_id] || {}).currency || '';
        const termText = r.terminator_name || '— (активный)';
        return `<tr>
          <td>${esc2(r.client_name)}</td>
          <td><button class="manage-link-btn" onclick="openManageRateTermDlg(${r.id})" title="Сменить терминатора">${esc2(termText)}</button></td>
          <td>${esc2(r.destination_name)}</td>
          <td class="mut">${r.client_tech_prefix ? esc2(r.client_tech_prefix) : '—'}</td>
          <td class="mut">${esc2(r.prefix)}</td>
          <td class="right">${money4(r.sell_rate_cents, cur)}</td>
          <td class="right"><button class="small danger" onclick="delRate(${r.id})">✕</button></td>
        </tr>`;
      }).join('') || '<tr><td class="empty" colspan="7">Нет данных</td></tr>';
    }
  }

  async function renderManageControls() {
    try {
      const d = await fetchManageData();
      renderManageTables(d);
    } catch (err) {
      console.error('manage controls failed', err);
    }
  }

  window.editManageClientName = async id => {
    const c = state.clients[id];
    if (!c) return;
    const name = prompt('Новое имя оригинатора', c.name || '');
    if (name === null) return;
    const clean = name.trim();
    if (!clean || clean === c.name) return;
    try {
      await manageApi(`/api/clients/${id}`, 'PATCH', {name: clean});
      await load(true);
    } catch (err) { alert(err.message); }
  };

  window.deleteManageClient = async id => {
    const c = state.clients[id] || {};
    if (!confirm(`Удалить оригинатора "${c.name || id}" из рабочего списка? Его роуты будут удалены, история CDR останется.`)) return;
    try {
      await manageApi(`/api/clients/${id}`, 'DELETE');
      await load(true);
    } catch (err) { alert(err.message); }
  };

  window.editManageGroupName = async id => {
    const g = state.groups[id];
    if (!g) return;
    const name = prompt('Новое имя терминационной группы', g.name || '');
    if (name === null) return;
    const clean = name.trim();
    if (!clean || clean === g.name) return;
    try {
      await manageApi(`/api/termination-groups/${id}`, 'PATCH', {name: clean});
      await load(true);
    } catch (err) { alert(err.message); }
  };

  window.openManageRateTermDlg = id => {
    const r = state.rates[id];
    if (!r) return;
    editingRateId = id;
    const sameDirection = t =>
      String(t.prefix || '') === String(r.prefix || '') ||
      String(t.destination_name || '').trim().toLowerCase() === String(r.destination_name || '').trim().toLowerCase();
    const candidates = state.terms.filter(sameDirection);
    const options = candidates.length ? candidates : state.terms;
    const sel = document.getElementById('manage-rate-term-select');
    sel.innerHTML = options.map(t => {
      const selected = Number(t.id) === Number(r.terminator_id) ? ' selected' : '';
      return `<option value="${t.id}"${selected}>${esc2(termLabel(t))}</option>`;
    }).join('');
    if (!sel.innerHTML) { alert('Сначала создайте терминатор'); return; }
    document.getElementById('manage-rate-term-hint').textContent =
      `Роут: ${r.client_name} → ${r.destination_name} / ${r.prefix}. Выбирай терминатора для этого направления.`;
    document.getElementById('manage-rate-term-dlg').showModal();
  };

  document.getElementById('manage-rate-term-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (!editingRateId) return;
    try {
      await manageApi(`/api/client-rates/${editingRateId}`, 'PATCH', {
        terminator_id: parseInt(document.getElementById('manage-rate-term-select').value)
      });
      document.getElementById('manage-rate-term-dlg').close();
      editingRateId = null;
      await load(true);
    } catch (err) { alert(err.message); }
  });

  if (typeof load === 'function') {
    const originalLoad = load;
    load = async function(manual = false) {
      await originalLoad(manual);
      await renderManageControls();
    };
    load(true);
  } else {
    renderManageControls();
  }
})();
</script>"""
