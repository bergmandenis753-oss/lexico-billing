from pathlib import Path

from fastapi.responses import HTMLResponse

from credit_limit_common import no_store


CREDIT_DASHBOARD_INJECTION = r"""
<style>
  .money-neg { color: var(--bad); }
  .client-actions { display:flex; gap:8px; justify-content:flex-end; flex-wrap:wrap; }
</style>
<script>
(function () {
  if (window.__creditLimitPatch) return;
  window.__creditLimitPatch = true;
  let creditClientId = null;
  const safeMoney = (units, cur) => {
    try { return money(units, cur); }
    catch (_) { return ((Number(units) || 0) / 10000).toFixed(4) + (cur ? ' ' + cur : ''); }
  };
  const moneyClass = units => Number(units || 0) < 0 ? 'money-neg' : '';
  const moneyInputValue = units => ((Number(units) || 0) / MONEY_SCALE).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') || '0';
  const getClients = () => { try { return Object.values(clientMap || {}); } catch (_) { return []; } };

  function ensureCreditDialog() {
    if (!document.getElementById('credit-dlg')) {
      document.body.insertAdjacentHTML('beforeend', `
        <dialog id="credit-dlg">
          <h3>Кредитный лимит</h3>
          <form id="credit-form">
            <label>Клиент</label><input id="cr-client" disabled>
            <label>Текущий баланс</label><input id="cr-balance" disabled>
            <label>Лимит кредита (в валюте)</label><input id="cr-limit" type="number" step="0.0001" min="0" required autofocus>
            <div class="row"><button type="button" class="ghost" onclick="document.getElementById('credit-dlg').close()">Отмена</button><button type="submit">Сохранить</button></div>
          </form>
        </dialog>`);
      document.getElementById('credit-form').addEventListener('submit', async e => {
        e.preventDefault();
        try {
          await api(`/api/clients/${creditClientId}`, 'PATCH', {
            credit_limit_cents: inputMoneyUnits('cr-limit', 'Кредитный лимит')
          });
          document.getElementById('credit-dlg').close();
          await load(true);
        } catch (err) { alert('Ошибка: ' + err.message); }
      });
    }
    const balanceInput = document.getElementById('cl-bal');
    if (balanceInput && !document.getElementById('cl-credit')) {
      balanceInput.insertAdjacentHTML('afterend',
        '<label>Кредитный лимит (в валюте)</label><input id="cl-credit" type="number" step="0.0001" min="0" value="0">');
    }
  }

  function hookClientCreate() {
    const form = document.getElementById('client-form');
    if (!form || form.__creditHooked) return;
    form.__creditHooked = true;
    form.addEventListener('submit', async e => {
      const creditInput = document.getElementById('cl-credit');
      let credit = 0;
      try { credit = creditInput ? inputMoneyUnits('cl-credit', 'Кредитный лимит') : 0; }
      catch (err) {
        e.preventDefault();
        e.stopImmediatePropagation();
        alert('Ошибка: ' + err.message);
        return;
      }
      if (!credit) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      try {
        await api('/api/clients', 'POST', {
          name: document.getElementById('cl-name').value,
          sip_ip: document.getElementById('cl-ip').value,
          currency: document.getElementById('cl-cur').value || 'USD',
          balance_cents: inputMoneyUnits('cl-bal', 'Баланс'),
          credit_limit_cents: credit
        });
        clientDlg.close();
        e.target.reset();
        await load(true);
      } catch (err) { alert('Ошибка: ' + err.message); }
    }, true);
  }

  function renderCreditClients() {
    const tbody = document.getElementById('t-clients');
    if (!tbody) return;
    const table = tbody.closest('table');
    if (table && !table.__creditHeader) {
      const head = table.querySelector('thead tr');
      if (head) {
        head.innerHTML = '<th>Имя</th><th>IP</th><th class="right">Баланс</th><th class="right">Кредитный лимит</th><th class="right">Доступно</th><th>Статус</th><th></th>';
        table.__creditHeader = true;
      }
    }
    const clients = getClients();
    tbody.innerHTML = clients.map(c => {
      const limit = Number(c.credit_limit_cents || 0);
      const available = c.available_balance_cents == null ? Number(c.balance_cents || 0) + limit : Number(c.available_balance_cents || 0);
      return `
        <tr>
          <td>${esc(c.name)}</td>
          <td class="mut">${esc(c.sip_ip)}</td>
          <td class="right ${moneyClass(c.balance_cents)}">${safeMoney(c.balance_cents, c.currency)}</td>
          <td class="right">${safeMoney(limit, c.currency)}</td>
          <td class="right ${moneyClass(available)}">${safeMoney(available, c.currency)}</td>
          <td>${c.active ? '<span class="badge on">активен</span>' : '<span class="badge off">выкл</span>'}</td>
          <td class="right"><span class="client-actions"><button class="small ghost" onclick="openCredit(${c.id})">Кредит</button><button class="small" onclick="openTopup(${c.id})">+ Пополнить</button></span></td>
        </tr>`;
    }).join('') || `<tr><td class="empty" colspan="7">Нет данных</td></tr>`;
  }

  window.openCredit = function (id) {
    ensureCreditDialog();
    creditClientId = id;
    const client = getClients().find(c => Number(c.id) === Number(id)) || {};
    document.getElementById('cr-client').value = client.name || ('#' + id);
    document.getElementById('cr-balance').value = safeMoney(client.balance_cents, client.currency || '');
    document.getElementById('cr-limit').value = moneyInputValue(client.credit_limit_cents || 0);
    document.getElementById('credit-dlg').showModal();
  };

  function installLoadHook() {
    try {
      if (typeof load !== 'function' || load.__creditWrapped) return;
      const originalLoad = load;
      load = async function () {
        const result = await originalLoad.apply(this, arguments);
        ensureCreditDialog();
        hookClientCreate();
        renderCreditClients();
        return result;
      };
      load.__creditWrapped = true;
    } catch (_) {}
  }
  function boot() {
    ensureCreditDialog();
    hookClientCreate();
    installLoadHook();
    renderCreditClients();
    setTimeout(renderCreditClients, 500);
    setTimeout(() => { try { load(true); } catch (_) {} }, 900);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>
"""


def dashboard_html():
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
    if "__creditLimitPatch" not in html:
        html = html.replace("</body>", CREDIT_DASHBOARD_INJECTION + "\n</body>", 1)
    return no_store(HTMLResponse(html))
