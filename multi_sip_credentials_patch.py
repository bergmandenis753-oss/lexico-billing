import ipaddress
import re
import secrets
import string
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from credit_limit_common import client_row, remove_routes


SIP_PROFILE = "lexico-users"
SIP_SERVER = "207.154.192.34"
SIP_PORT = 5070
LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{4,64}$")
PASSWORD_ALPHABET = string.ascii_letters + string.digits


class ClientCreateIn(BaseModel):
    name: str
    sip_ip: str = ""
    connection_mode: str = "ip"
    currency: str = "USD"
    balance_cents: int = 0
    credit_limit_cents: int = 0
    active: bool = True


class SipCredentialCreateIn(BaseModel):
    label: str = ""
    sip_login: str = ""


class SipCredentialUpdateIn(BaseModel):
    label: Optional[str] = None
    active: Optional[bool] = None


class ClientIpsUpdateIn(BaseModel):
    sip_ip: str


def _password(length=20):
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _login(client_id):
    return f"u{client_id}_{secrets.token_hex(4)}"


def _normalize_mode(value, sip_ip=""):
    mode = str(value or "").strip().lower()
    if mode not in {"ip", "sip", "ip_sip"}:
        mode = "ip" if str(sip_ip or "").strip() else "sip"
    return mode


def _normalize_ip_list(value):
    tokens = [token for token in re.split(r"[\s,;]+", str(value or "").strip()) if token]
    if not tokens:
        raise HTTPException(400, "Укажите хотя бы один IP")
    if len(tokens) > 100:
        raise HTTPException(400, "Можно указать не больше 100 IP")

    normalized = []
    seen = set()
    for token in tokens:
        try:
            if "/" in token:
                item = ipaddress.ip_network(token, strict=False).with_prefixlen
            else:
                item = ipaddress.ip_address(token).compressed
        except ValueError:
            raise HTTPException(400, f"Некорректный IP: {token}")
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


def _as_network(token):
    try:
        if "/" in token:
            return ipaddress.ip_network(token, strict=False)
        address = ipaddress.ip_address(token)
        return ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=False)
    except ValueError:
        return None


def _find_ip_conflict(conn, client_id, tokens):
    wanted = [_as_network(token) for token in tokens]
    rows = conn.execute(
        "SELECT id, name, sip_ip FROM clients "
        "WHERE id != ? AND COALESCE(deleted_at, '') = ''",
        (client_id,),
    ).fetchall()
    for row in rows:
        for existing_token in re.split(r"[\s,;]+", str(row["sip_ip"] or "").strip()):
            existing = _as_network(existing_token)
            if existing is None:
                continue
            if any(network.version == existing.version and network.overlaps(existing) for network in wanted):
                return row, existing_token
    return None


def _public_credential(row):
    item = dict(row)
    item.update({"server": SIP_SERVER, "port": SIP_PORT, "transport": "UDP"})
    item["active"] = bool(item.get("active"))
    return item


def ensure_schema(db):
    conn = db.get_conn()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
        if "deleted_at" not in columns:
            conn.execute("ALTER TABLE clients ADD COLUMN deleted_at TEXT")
        if "connection_mode" not in columns:
            conn.execute("ALTER TABLE clients ADD COLUMN connection_mode TEXT NOT NULL DEFAULT 'ip'")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_sip_credentials (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                label        TEXT NOT NULL DEFAULT '',
                sip_login    TEXT NOT NULL UNIQUE,
                sip_password TEXT NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_client_sip_credentials_client
                ON client_sip_credentials(client_id, active);
            """
        )
        conn.commit()
    finally:
        conn.close()


def resolve_client(conn, data, db):
    sip_login = str(getattr(data, "sip_login", "") or "").strip()
    profile = str(getattr(data, "profile", "") or "").strip()
    if profile == SIP_PROFILE and sip_login:
        return conn.execute(
            "SELECT c.* FROM client_sip_credentials sc "
            "JOIN clients c ON c.id = sc.client_id "
            "WHERE sc.sip_login = ? AND sc.active = 1 "
            "AND c.active = 1 AND COALESCE(c.deleted_at, '') = ''",
            (sip_login,),
        ).fetchone()
    return db.get_client_by_ip(conn, data.sip_ip)


def _client_or_404(conn, client_id):
    row = conn.execute(
        "SELECT * FROM clients WHERE id = ? AND COALESCE(deleted_at, '') = ''", (client_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Оригинатор не найден")
    return row


SIP_DASHBOARD_INJECTION = r"""
<style>
  .sip-actions { display:flex; gap:8px; justify-content:flex-end; align-items:center; flex-wrap:wrap; }
  .sip-mode-row { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:13px; }
  .sip-mode-row button { background:transparent; border:1px solid var(--line); color:var(--txt); }
  .sip-mode-row button.active { background:var(--accent); border-color:var(--accent); color:#fff; }
  .sip-credentials { display:grid; gap:10px; margin:14px 0; max-height:52vh; overflow:auto; }
  .sip-credential { border:1px solid var(--line); border-radius:7px; padding:12px; }
  .sip-credential-head { display:flex; justify-content:space-between; gap:12px; align-items:center; }
  .sip-credential-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px; }
  .sip-value { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; word-break:break-all; }
  .sip-empty { color:var(--mut); padding:14px 0; }
  .client-ip-edit { width:30px; height:30px; padding:0; font-size:17px; line-height:1; }
  #client-ip-dlg { width:min(480px, calc(100vw - 32px)); }
  #client-ip-list { width:100%; min-height:210px; resize:vertical; padding:9px; border:1px solid var(--line); border-radius:7px; background:var(--bg); color:var(--txt); margin-bottom:8px; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
  #client-ip-error { min-height:20px; color:var(--bad); white-space:pre-wrap; overflow-wrap:anywhere; }
  @media (max-width:700px) { .sip-credential-grid { grid-template-columns:1fr; } }
</style>
<dialog id="client-ip-dlg">
  <h3>IP аккаунта</h3>
  <p id="client-ip-name" class="mut"></p>
  <form onsubmit="saveClientIps(event)">
    <label for="client-ip-list">IP-адреса, по одному в строке</label>
    <textarea id="client-ip-list" required spellcheck="false" autocomplete="off"></textarea>
    <div id="client-ip-error"></div>
    <div class="row">
      <button type="button" class="ghost" onclick="document.getElementById('client-ip-dlg').close()">Отмена</button>
      <button type="submit">Сохранить</button>
    </div>
  </form>
</dialog>
<dialog id="sip-credentials-dlg" class="wide">
  <h3>SIP доступы</h3>
  <p id="sip-client-name" class="mut"></p>
  <div id="sip-credentials-list" class="sip-credentials"></div>
  <div class="row">
    <button type="button" class="ghost" onclick="document.getElementById('sip-credentials-dlg').close()">Закрыть</button>
    <button type="button" onclick="createSipCredential()">+ Добавить логин</button>
  </div>
</dialog>
<script>
(function () {
  if (window.__multiSipCredentialsPatch) return;
  window.__multiSipCredentialsPatch = true;
  let sipClientId = null;
  let ipClientId = null;
  let createMode = 'ip';
  const getClients = () => { try { return Object.values(clientMap || {}); } catch (_) { return []; } };
  const safeMoney = (units, cur) => {
    try { return money(units, cur); }
    catch (_) { return ((Number(units) || 0) / 10000).toFixed(4) + (cur ? ' ' + cur : ''); }
  };
  const moneyClass = units => Number(units || 0) < 0 ? 'money-neg' : '';

  function applyCreateMode(mode) {
    createMode = mode === 'sip' ? 'sip' : 'ip';
    window.__sipCreateMode = createMode;
    const ip = document.getElementById('cl-ip');
    if (ip) {
      ip.required = createMode === 'ip';
      ip.style.display = createMode === 'ip' ? '' : 'none';
      const label = ip.previousElementSibling;
      if (label && label.tagName === 'LABEL') label.style.display = createMode === 'ip' ? '' : 'none';
      if (createMode === 'sip') ip.value = '';
    }
    document.querySelectorAll('[data-sip-create-mode]').forEach(btn =>
      btn.classList.toggle('active', btn.dataset.sipCreateMode === createMode));
  }

  function ensureCreateMode() {
    const name = document.getElementById('cl-name');
    if (!name || document.getElementById('cl-mode-row')) return;
    name.insertAdjacentHTML('beforebegin', `
      <label>Подключение</label>
      <div class="sip-mode-row" id="cl-mode-row">
        <button type="button" class="active" data-sip-create-mode="ip">По IP</button>
        <button type="button" data-sip-create-mode="sip">SIP логин</button>
      </div>`);
    document.querySelectorAll('[data-sip-create-mode]').forEach(btn =>
      btn.addEventListener('click', () => applyCreateMode(btn.dataset.sipCreateMode)));
    document.getElementById('client-dlg')?.addEventListener('close', () => applyCreateMode('ip'));
    applyCreateMode('ip');
  }

  function hookClientCreate() {
    const form = document.getElementById('client-form');
    if (!form || form.__sipModeHooked) return;
    form.__sipModeHooked = true;
    form.addEventListener('submit', async e => {
      e.preventDefault();
      e.stopImmediatePropagation();
      try {
        await api('/api/clients', 'POST', {
          name: document.getElementById('cl-name').value,
          sip_ip: createMode === 'ip' ? document.getElementById('cl-ip').value : '',
          connection_mode: createMode,
          currency: document.getElementById('cl-cur').value || 'USD',
          balance_cents: inputMoneyUnits('cl-bal', 'Баланс'),
          credit_limit_cents: document.getElementById('cl-credit') ? inputMoneyUnits('cl-credit', 'Кредитный лимит') : 0
        });
        clientDlg.close();
        e.target.reset();
        applyCreateMode('ip');
        await load(true);
      } catch (err) { alert('Ошибка: ' + err.message); }
    }, true);
  }

  function connectionLabel(c) {
    const mode = String(c.connection_mode || 'ip');
    if (mode === 'sip') return '<span class="badge on">SIP логины</span>';
    if (mode === 'ip_sip') return `<span class="badge on">IP + SIP</span><div class="mut">${esc(c.sip_ip || '')}</div>`;
    return `<span class="mut">${esc(c.sip_ip || '')}</span>`;
  }

  function ipEditButton(c) {
    const mode = String(c.connection_mode || 'ip');
    if (mode === 'sip') return '';
    return `<button class="small ghost client-ip-edit" title="Редактировать IP" aria-label="Редактировать IP ${esc(c.name)}" onclick="openClientIpEditor(${c.id})">&#9998;</button>`;
  }

  function renderClients() {
    const tbody = document.getElementById('t-clients');
    if (!tbody) return;
    const head = tbody.closest('table')?.querySelector('thead tr');
    if (head) head.innerHTML = '<th>Имя</th><th>Подключение</th><th class="right">Баланс</th><th class="right">Кредитный лимит</th><th class="right">Доступно</th><th>Статус</th><th></th>';
    tbody.innerHTML = getClients().map(c => {
      const limit = Number(c.credit_limit_cents || 0);
      const available = c.available_balance_cents == null ? Number(c.balance_cents || 0) + limit : Number(c.available_balance_cents || 0);
      return `<tr>
        <td>${esc(c.name)}</td><td>${connectionLabel(c)}</td>
        <td class="right ${moneyClass(c.balance_cents)}">${safeMoney(c.balance_cents, c.currency)}</td>
        <td class="right">${safeMoney(limit, c.currency)}</td>
        <td class="right ${moneyClass(available)}">${safeMoney(available, c.currency)}</td>
        <td>${c.active ? '<span class="badge on">активен</span>' : '<span class="badge off">выкл</span>'}</td>
        <td class="right"><span class="sip-actions">${ipEditButton(c)}<button class="small ghost" onclick="openSipCredentials(${c.id})">SIP доступы</button><button class="small ghost" onclick="openCredit(${c.id})">Кредит</button><button class="small" onclick="openTopup(${c.id})">+ Пополнить</button></span></td>
      </tr>`;
    }).join('') || '<tr><td class="empty" colspan="7">Нет данных</td></tr>';
  }

  function copyText(value) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
    const input = document.createElement('textarea'); input.value = value; document.body.appendChild(input);
    input.select(); document.execCommand('copy'); input.remove(); return Promise.resolve();
  }

  async function refreshCredentials() {
    const list = document.getElementById('sip-credentials-list');
    list.innerHTML = '<div class="sip-empty">Загрузка...</div>';
    try {
      const rows = await api(`/api/clients/${sipClientId}/sip-credentials`, 'GET');
      list.innerHTML = rows.map(item => `<div class="sip-credential">
        <div class="sip-credential-head"><strong>${esc(item.label || item.sip_login)}</strong>${item.active ? '<span class="badge on">активен</span>' : '<span class="badge off">выкл</span>'}</div>
        <div class="sip-credential-grid">
          <div><div class="mut">Логин</div><div class="sip-value">${esc(item.sip_login)}</div></div>
          <div><div class="mut">Пароль</div><div class="sip-value">${esc(item.sip_password)}</div></div>
          <div><div class="mut">Сервер</div><div class="sip-value">${esc(item.server)}:${item.port}</div></div>
          <div><div class="mut">Транспорт</div><div class="sip-value">${esc(item.transport)}</div></div>
        </div>
        <div class="sip-actions" style="margin-top:12px">
          <button class="small ghost" onclick="copySipSettings(${item.id})">Копировать</button>
          <button class="small ghost" onclick="toggleSipCredential(${item.id}, ${item.active ? 'false' : 'true'})">${item.active ? 'Выключить' : 'Включить'}</button>
          <button class="small ghost" onclick="regenerateSipPassword(${item.id})">Новый пароль</button>
          <button class="small danger" onclick="deleteSipCredential(${item.id})">Удалить</button>
        </div>
      </div>`).join('') || '<div class="sip-empty">Пока нет SIP-логинов.</div>';
      window.__sipCredentialRows = Object.fromEntries(rows.map(x => [x.id, x]));
    } catch (err) { list.innerHTML = `<div class="sip-empty">Ошибка: ${esc(err.message)}</div>`; }
  }

  window.openSipCredentials = async function (id) {
    sipClientId = id;
    const client = getClients().find(c => Number(c.id) === Number(id)) || {};
    document.getElementById('sip-client-name').textContent = client.name || ('#' + id);
    document.getElementById('sip-credentials-dlg').showModal();
    await refreshCredentials();
  };
  window.openClientIpEditor = function (id) {
    const client = getClients().find(c => Number(c.id) === Number(id));
    if (!client) return;
    ipClientId = id;
    document.getElementById('client-ip-name').textContent = client.name || ('#' + id);
    const input = document.getElementById('client-ip-list');
    input.value = String(client.sip_ip || '').split(/\s*[,;]\s*/).filter(Boolean).join('\n');
    document.getElementById('client-ip-error').textContent = '';
    document.getElementById('client-ip-dlg').showModal();
    requestAnimationFrame(() => { input.setSelectionRange(0, 0); input.scrollTop = 0; });
  };
  window.saveClientIps = async function (event) {
    event.preventDefault();
    const error = document.getElementById('client-ip-error');
    const submit = event.target.querySelector('button[type="submit"]');
    error.textContent = '';
    submit.disabled = true;
    try {
      await api(`/api/clients/${ipClientId}/ips`, 'PUT', {
        sip_ip: document.getElementById('client-ip-list').value
      });
      document.getElementById('client-ip-dlg').close();
      await load(true);
    } catch (err) {
      error.textContent = err.message;
    } finally {
      submit.disabled = false;
    }
  };
  window.createSipCredential = async function () {
    const label = prompt('Название агента или рабочего места:', '') ?? null;
    if (label === null) return;
    try { await api(`/api/clients/${sipClientId}/sip-credentials`, 'POST', {label}); await refreshCredentials(); }
    catch (err) { alert('Ошибка: ' + err.message); }
  };
  window.toggleSipCredential = async function (id, active) {
    try { await api(`/api/client-sip-credentials/${id}`, 'PATCH', {active}); await refreshCredentials(); }
    catch (err) { alert('Ошибка: ' + err.message); }
  };
  window.regenerateSipPassword = async function (id) {
    if (!confirm('Сменить пароль? Старый сразу перестанет работать.')) return;
    try { await api(`/api/client-sip-credentials/${id}/regenerate-password`, 'POST'); await refreshCredentials(); }
    catch (err) { alert('Ошибка: ' + err.message); }
  };
  window.deleteSipCredential = async function (id) {
    if (!confirm('Удалить этот SIP-доступ?')) return;
    try { await api(`/api/client-sip-credentials/${id}`, 'DELETE'); await refreshCredentials(); }
    catch (err) { alert('Ошибка: ' + err.message); }
  };
  window.copySipSettings = async function (id) {
    const item = (window.__sipCredentialRows || {})[id]; if (!item) return;
    await copyText(`Сервер: ${item.server}\nПорт: ${item.port}\nЛогин: ${item.sip_login}\nПароль: ${item.sip_password}\nТранспорт: ${item.transport}`);
  };

  function installLoadHook() {
    if (typeof load !== 'function' || load.__sipCredentialsWrapped) return;
    const originalLoad = load;
    load = async function () { const result = await originalLoad.apply(this, arguments); renderClients(); return result; };
    load.__sipCredentialsWrapped = true;
  }
  function boot() {
    ensureCreateMode(); hookClientCreate(); installLoadHook(); renderClients();
    setTimeout(renderClients, 700); setTimeout(() => { try { load(true); } catch (_) {} }, 1100);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
</script>
"""


def _install_dashboard_injection():
    import credit_limit_ui

    if "__multiSipCredentialsPatch" not in credit_limit_ui.CREDIT_DASHBOARD_INJECTION:
        credit_limit_ui.CREDIT_DASHBOARD_INJECTION += "\n" + SIP_DASHBOARD_INJECTION


def install(app, main, db):
    ensure_schema(db)
    db.get_client_for_sip_request = lambda conn, data: resolve_client(conn, data, db)
    _install_dashboard_injection()

    @app.on_event("startup")
    def _multi_sip_startup():
        ensure_schema(db)

    remove_routes(app, "/api/clients", {"POST"})

    @app.post("/api/clients", dependencies=main.ADMIN_WRITE_AUTH)
    def create_client(data: ClientCreateIn):
        ensure_schema(db)
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "Укажите имя оригинатора")
        mode = _normalize_mode(data.connection_mode, data.sip_ip)
        sip_ip = data.sip_ip.strip()
        if mode in {"ip", "ip_sip"} and not sip_ip:
            raise HTTPException(400, "Для подключения по IP укажите SIP IP")
        if not sip_ip:
            sip_ip = f"sip-only:{secrets.token_hex(12)}"
        if data.balance_cents < 0 or data.credit_limit_cents < 0:
            raise HTTPException(400, "Баланс и кредитный лимит не могут быть отрицательными")
        conn = db.get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO clients (name, sip_ip, connection_mode, balance_cents, credit_limit_cents, currency, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, sip_ip, mode, data.balance_cents, data.credit_limit_cents, data.currency, int(data.active)),
            )
            row = conn.execute("SELECT * FROM clients WHERE id = ?", (cur.lastrowid,)).fetchone()
            conn.commit()
            return {"id": cur.lastrowid, "client": client_row(row)}
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, f"IP {data.sip_ip} уже используется")
        finally:
            conn.close()

    @app.put("/api/clients/{client_id}/ips", dependencies=main.ADMIN_WRITE_AUTH)
    def update_client_ips(client_id: int, data: ClientIpsUpdateIn):
        ensure_schema(db)
        tokens = _normalize_ip_list(data.sip_ip)
        normalized = ", ".join(tokens)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            client = _client_or_404(conn, client_id)
            if _normalize_mode(client["connection_mode"], client["sip_ip"]) == "sip":
                conn.rollback()
                raise HTTPException(409, "Для SIP-аккаунта IP не используется")
            conflict = _find_ip_conflict(conn, client_id, tokens)
            if conflict is not None:
                row, ip = conflict
                conn.rollback()
                raise HTTPException(409, f"IP {ip} уже используется аккаунтом {row['name']}")
            conn.execute("UPDATE clients SET sip_ip = ? WHERE id = ?", (normalized, client_id))
            row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            conn.commit()
            return {"ok": True, "client": client_row(row)}
        except db.sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(409, "Такой список IP уже используется")
        finally:
            conn.close()

    @app.get("/api/clients/{client_id}/sip-credentials", dependencies=main.ADMIN_AUTH)
    def list_credentials(client_id: int):
        conn = db.get_conn()
        try:
            _client_or_404(conn, client_id)
            rows = conn.execute(
                "SELECT * FROM client_sip_credentials WHERE client_id = ? ORDER BY id", (client_id,)
            ).fetchall()
            return [_public_credential(row) for row in rows]
        finally:
            conn.close()

    @app.post("/api/clients/{client_id}/sip-credentials", dependencies=main.ADMIN_WRITE_AUTH)
    def create_credential(client_id: int, data: SipCredentialCreateIn):
        ensure_schema(db)
        conn = db.get_conn()
        try:
            _client_or_404(conn, client_id)
            login = data.sip_login.strip() or _login(client_id)
            if not LOGIN_RE.fullmatch(login):
                raise HTTPException(400, "Логин: 4-64 символа, латиница, цифры, точка, дефис или _")
            password = _password()
            cur = conn.execute(
                "INSERT INTO client_sip_credentials (client_id, label, sip_login, sip_password) VALUES (?, ?, ?, ?)",
                (client_id, data.label.strip()[:100], login, password),
            )
            conn.execute(
                "UPDATE clients SET connection_mode = CASE WHEN connection_mode = 'ip' THEN 'ip_sip' ELSE 'sip' END "
                "WHERE id = ?",
                (client_id,),
            )
            row = conn.execute("SELECT * FROM client_sip_credentials WHERE id = ?", (cur.lastrowid,)).fetchone()
            conn.commit()
            return _public_credential(row)
        except db.sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(409, "Такой SIP-логин уже существует")
        finally:
            conn.close()

    @app.patch("/api/client-sip-credentials/{credential_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def update_credential(credential_id: int, data: SipCredentialUpdateIn):
        fields = data.model_dump(exclude_none=True) if hasattr(data, "model_dump") else data.dict(exclude_none=True)
        if not fields:
            raise HTTPException(400, "Нет изменений")
        if "label" in fields:
            fields["label"] = str(fields["label"] or "").strip()[:100]
        if "active" in fields:
            fields["active"] = int(bool(fields["active"]))
        conn = db.get_conn()
        try:
            sets = ", ".join(f"{key} = ?" for key in fields)
            cur = conn.execute(
                f"UPDATE client_sip_credentials SET {sets}, updated_at = datetime('now') WHERE id = ?",
                (*fields.values(), credential_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(404, "SIP-доступ не найден")
            row = conn.execute("SELECT * FROM client_sip_credentials WHERE id = ?", (credential_id,)).fetchone()
            conn.commit()
            return _public_credential(row)
        finally:
            conn.close()

    @app.post("/api/client-sip-credentials/{credential_id}/regenerate-password", dependencies=main.ADMIN_WRITE_AUTH)
    def regenerate_password(credential_id: int):
        conn = db.get_conn()
        try:
            password = _password()
            cur = conn.execute(
                "UPDATE client_sip_credentials SET sip_password = ?, updated_at = datetime('now') WHERE id = ?",
                (password, credential_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(404, "SIP-доступ не найден")
            row = conn.execute("SELECT * FROM client_sip_credentials WHERE id = ?", (credential_id,)).fetchone()
            conn.commit()
            return _public_credential(row)
        finally:
            conn.close()

    @app.delete("/api/client-sip-credentials/{credential_id}", dependencies=main.ADMIN_WRITE_AUTH)
    def delete_credential(credential_id: int):
        conn = db.get_conn()
        try:
            cur = conn.execute("DELETE FROM client_sip_credentials WHERE id = ?", (credential_id,))
            if cur.rowcount == 0:
                raise HTTPException(404, "SIP-доступ не найден")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @app.get("/api/freeswitch/sip-credentials", dependencies=main.API_AUTH)
    def freeswitch_credentials():
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT sc.id, sc.client_id, sc.sip_login, sc.sip_password, c.name AS client_name "
                "FROM client_sip_credentials sc JOIN clients c ON c.id = sc.client_id "
                "WHERE sc.active = 1 AND c.active = 1 AND COALESCE(c.deleted_at, '') = '' ORDER BY sc.id"
            ).fetchall()
            return {
                "ok": True,
                "profile": SIP_PROFILE,
                "server": SIP_SERVER,
                "port": SIP_PORT,
                "credentials": [dict(row) for row in rows],
            }
        finally:
            conn.close()
