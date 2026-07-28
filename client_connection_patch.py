import os
import secrets
import string
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse


SERVER_HOST = os.getenv("PUBLIC_SIP_HOST", "207.154.192.34")
SERVER_PORT = os.getenv("PUBLIC_SIP_PORT", "5060")
AUTH_MODES = {"ip", "sip", "ip_sip"}


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


def _clean(value):
    return str(value or "").strip()


def _random_password(length=18):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _ensure_columns(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
    if "auth_mode" not in columns:
        conn.execute("ALTER TABLE clients ADD COLUMN auth_mode TEXT NOT NULL DEFAULT 'ip'")
    if "sip_login" not in columns:
        conn.execute("ALTER TABLE clients ADD COLUMN sip_login TEXT NOT NULL DEFAULT ''")
    if "sip_password" not in columns:
        conn.execute("ALTER TABLE clients ADD COLUMN sip_password TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_sip_login ON clients(sip_login) WHERE sip_login <> ''")


def _connection_message(client):
    mode = client["auth_mode"] if "auth_mode" in client.keys() else "ip"
    lines = [
        f"Данные для подключения SloTELL",
        f"Аккаунт: {client['name']}",
        f"SIP server: {SERVER_HOST}:{SERVER_PORT}",
    ]
    if mode in {"ip", "ip_sip"}:
        lines += [
            "",
            "Вариант IP to IP:",
            f"Ваш whitelist IP: {client['sip_ip']}",
            "Отправлять INVITE на наш сервер по SIP UDP 5060.",
        ]
    if mode in {"sip", "ip_sip"}:
        lines += [
            "",
            "Вариант SIP login/password:",
            f"Login: {client['sip_login']}",
            f"Password: {client['sip_password']}",
            f"Domain/Proxy: {SERVER_HOST}",
            f"Port: {SERVER_PORT}",
            "Transport: UDP",
        ]
    lines += [
        "",
        "Номер отправлять в формате: tech prefix + E.164 номер, если tech prefix выдан для направления.",
    ]
    return "\n".join(lines)


def _patch_dashboard_html(html):
    html = html.replace(
        '<div class="sec-head"><h2>Оригинаторы (кому даём роут)</h2><button class="small" onclick="clientDlg.showModal()">+ Оригинатор</button></div>',
        '<div class="sec-head"><h2>Оригинаторы (кому даём роут)</h2><button class="small" onclick="openClientDlg()">+ Оригинатор</button></div>',
    )
    html = html.replace(
        "<thead><tr><th>Имя</th><th>IP</th><th class=\"right\">Баланс</th><th>Статус</th><th></th></tr></thead>",
        "<thead><tr><th>Имя</th><th>Подключение</th><th class=\"right\">Баланс</th><th>Статус</th><th></th></tr></thead>",
    )
    html = html.replace(
        """<dialog id="client-dlg">
  <h3>Новый оригинатор</h3>
  <form id="client-form">
    <label>Имя</label><input id="cl-name" required autofocus>
    <label>SIP IP (whitelist)</label><input id="cl-ip" placeholder="203.0.113.10" required>
    <label>Валюта</label><input id="cl-cur" value="USD" required>
    <label>Стартовый баланс (в валюте)</label><input id="cl-bal" type="number" step="0.01" min="0" value="0">
    <div class="row"><button type="button" class="ghost" onclick="clientDlg.close()">Отмена</button><button type="submit">Создать</button></div>
  </form>
</dialog>""",
        """<dialog id="client-dlg">
  <h3>Новый оригинатор</h3>
  <form id="client-form">
    <label>Имя</label><input id="cl-name" required autofocus>
    <label>Тип подключения</label>
    <select id="cl-auth-mode">
      <option value="ip">IP to IP</option>
      <option value="sip">SIP login/password</option>
      <option value="ip_sip">IP + SIP login/password</option>
    </select>
    <div id="cl-ip-wrap">
      <label>SIP IP (whitelist)</label><input id="cl-ip" placeholder="203.0.113.10">
    </div>
    <div id="cl-sip-wrap" style="display:none">
      <label>SIP login</label><input id="cl-login" placeholder="client101">
      <label>SIP password</label><input id="cl-pass" placeholder="сгенерируется автоматически">
      <button class="small ghost" type="button" onclick="generateSipCreds()">Сгенерировать SIP доступ</button>
    </div>
    <label>Валюта</label><input id="cl-cur" value="USD" required>
    <label>Стартовый баланс (в валюте)</label><input id="cl-bal" type="number" step="0.0001" min="0" value="0">
    <div class="row"><button type="button" class="ghost" onclick="clientDlg.close()">Отмена</button><button type="submit">Создать</button></div>
  </form>
</dialog>

<dialog id="client-msg-dlg" class="wide">
  <h3>Данные для клиента</h3>
  <textarea id="client-msg" readonly style="width:100%;min-height:260px;background:#0b1220;color:#e5edf8;border:1px solid #2b384d;border-radius:8px;padding:12px;font:14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace"></textarea>
  <div class="row"><button type="button" class="ghost" onclick="copyClientMessage()">Скопировать</button><button type="button" onclick="clientMsgDlg.close()">OK</button></div>
</dialog>""",
    )
    html = html.replace(
        "const clientDlg = document.getElementById('client-dlg');",
        "const clientDlg = document.getElementById('client-dlg');\nconst clientMsgDlg = document.getElementById('client-msg-dlg');",
    )
    html = html.replace(
        "async function load(manual = false) {",
        r"""function authModeLabel(c) {
  const mode = c.auth_mode || 'ip';
  if (mode === 'sip') return `SIP login: ${esc(c.sip_login || '')}`;
  if (mode === 'ip_sip') return `IP+SIP: ${esc(c.sip_ip || '')} / ${esc(c.sip_login || '')}`;
  return esc(c.sip_ip || '');
}

function openClientDlg() {
  document.getElementById('client-form').reset();
  document.getElementById('cl-cur').value = 'USD';
  document.getElementById('cl-bal').value = '0';
  syncClientAuthFields();
  clientDlg.showModal();
}

function syncClientAuthFields() {
  const mode = document.getElementById('cl-auth-mode').value;
  const ipOn = mode === 'ip' || mode === 'ip_sip';
  const sipOn = mode === 'sip' || mode === 'ip_sip';
  document.getElementById('cl-ip-wrap').style.display = ipOn ? '' : 'none';
  document.getElementById('cl-sip-wrap').style.display = sipOn ? '' : 'none';
  document.getElementById('cl-ip').required = ipOn;
  document.getElementById('cl-login').required = sipOn;
}

function tokenPart(len = 18) {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  const bytes = new Uint8Array(len);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, b => chars[b % chars.length]).join('');
}

function generateSipCreds() {
  const name = String(document.getElementById('cl-name').value || 'client').toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 16);
  if (!document.getElementById('cl-login').value) document.getElementById('cl-login').value = `${name || 'client'}${Math.floor(100 + Math.random() * 900)}`;
  document.getElementById('cl-pass').value = tokenPart(20);
}

function showClientMessage(text) {
  document.getElementById('client-msg').value = text || '';
  clientMsgDlg.showModal();
}

async function copyClientMessage() {
  await navigator.clipboard.writeText(document.getElementById('client-msg').value);
}

async function showClientConnection(id) {
  try {
    const r = await api(`/api/clients/${id}/connection-message`, 'GET');
    showClientMessage(r.message);
  } catch (e) { alert(e.message); }
}

document.addEventListener('change', e => {
  if (e.target && e.target.id === 'cl-auth-mode') syncClientAuthFields();
});

async function load(manual = false) {""",
    )
    html = html.replace(
        '<td class="mut">${esc(c.sip_ip)}</td>',
        '<td class="mut">${authModeLabel(c)}</td>',
    )
    html = html.replace(
        '<td class="right"><button class="small" onclick="openTopup(${c.id})">+ Пополнить</button></td>',
        '<td class="right"><button class="small ghost" onclick="showClientConnection(${c.id})">Данные</button> <button class="small" onclick="openTopup(${c.id})">+ Пополнить</button></td>',
    )
    html = html.replace(
        """document.getElementById('client-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await api('/api/clients', 'POST', {
      name: document.getElementById('cl-name').value,
      sip_ip: document.getElementById('cl-ip').value,
      currency: document.getElementById('cl-cur').value || 'USD',
      balance_cents: inputMoneyUnits('cl-bal', 'Баланс')
    });
    clientDlg.close(); e.target.reset(); load();
  } catch (err) { alert('Ошибка: ' + err.message); }
});""",
        """document.getElementById('client-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    const result = await api('/api/clients', 'POST', {
      name: document.getElementById('cl-name').value,
      auth_mode: document.getElementById('cl-auth-mode').value,
      sip_ip: document.getElementById('cl-ip').value,
      sip_login: document.getElementById('cl-login').value,
      sip_password: document.getElementById('cl-pass').value,
      currency: document.getElementById('cl-cur').value || 'USD',
      balance_cents: inputMoneyUnits('cl-bal', 'Баланс')
    });
    clientDlg.close(); e.target.reset(); load();
    if (result.connection_message) showClientMessage(result.connection_message);
  } catch (err) { alert('Ошибка: ' + err.message); }
});""",
    )
    return html


def _install_dashboard_route(app, main):
    _remove_routes(app, "/", {"GET"})

    @app.get("/", response_class=HTMLResponse, dependencies=main.ADMIN_AUTH)
    def patched_dashboard(request: Request):
        html = Path("dashboard.html").read_text(encoding="utf-8")
        response = HTMLResponse(_patch_dashboard_html(html))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def _normalize_client_payload(data):
    mode = _clean(data.get("auth_mode") or "ip")
    if mode not in AUTH_MODES:
        raise HTTPException(400, "Некорректный тип подключения")
    login = _clean(data.get("sip_login"))
    password = _clean(data.get("sip_password"))
    sip_ip = _clean(data.get("sip_ip"))
    if mode in {"ip", "ip_sip"} and not sip_ip:
        raise HTTPException(400, "Для IP to IP нужен SIP IP")
    if mode in {"sip", "ip_sip"}:
        if not login:
            raise HTTPException(400, "Для SIP login/password нужен login")
        if not password:
            password = _random_password()
    if mode == "sip" and not sip_ip:
        sip_ip = f"sip-login:{login}"
    if mode == "ip":
        login = ""
        password = ""
    return mode, sip_ip, login, password


def install(app, main, db):
    conn = db.get_conn()
    try:
        _ensure_columns(conn)
        conn.commit()
    finally:
        conn.close()

    _remove_routes(app, "/api/clients", {"POST"})
    _remove_routes(app, "/api/clients/{cid}", {"PATCH"})

    @app.post("/api/clients", dependencies=main.ADMIN_WRITE_AUTH)
    async def create_client(request: Request):
        data = await request.json()
        mode, sip_ip, login, password = _normalize_client_payload(data)
        balance = int(data.get("balance_cents") or 0)
        conn = db.get_conn()
        try:
            _ensure_columns(conn)
            cur = conn.execute(
                "INSERT INTO clients (name, sip_ip, balance_cents, currency, active, auth_mode, sip_login, sip_password) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _clean(data.get("name")),
                    sip_ip,
                    balance,
                    _clean(data.get("currency") or "USD"),
                    int(bool(data.get("active", True))),
                    mode,
                    login,
                    password,
                ),
            )
            conn.commit()
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (cur.lastrowid,)).fetchone()
            return {"id": cur.lastrowid, "connection_message": _connection_message(client)}
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, "IP или SIP login уже используется")
        finally:
            conn.close()

    @app.patch("/api/clients/{cid}", dependencies=main.ADMIN_WRITE_AUTH)
    async def update_client(cid: int, request: Request):
        data = await request.json()
        conn = db.get_conn()
        try:
            _ensure_columns(conn)
            old = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
            if old is None:
                raise HTTPException(404, "Клиент не найден")
            fields = {k: v for k, v in data.items() if v is not None}
            if any(k in fields for k in ("auth_mode", "sip_ip", "sip_login", "sip_password")):
                merged = dict(old)
                merged.update(fields)
                mode, sip_ip, login, password = _normalize_client_payload(merged)
                fields.update({"auth_mode": mode, "sip_ip": sip_ip, "sip_login": login, "sip_password": password})
            if "active" in fields:
                fields["active"] = int(bool(fields["active"]))
            allowed = {"name", "sip_ip", "currency", "active", "auth_mode", "sip_login", "sip_password"}
            fields = {k: v for k, v in fields.items() if k in allowed}
            if not fields:
                raise HTTPException(400, "Нет полей для обновления")
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*fields.values(), cid))
            conn.commit()
            return {"ok": True}
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, "IP или SIP login уже используется")
        finally:
            conn.close()

    @app.get("/api/clients/{cid}/connection-message", dependencies=main.ADMIN_AUTH)
    def client_connection_message(cid: int):
        conn = db.get_conn()
        try:
            _ensure_columns(conn)
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
            if client is None:
                raise HTTPException(404, "Клиент не найден")
            return {"message": _connection_message(client)}
        finally:
            conn.close()

    _install_dashboard_route(app, main)
