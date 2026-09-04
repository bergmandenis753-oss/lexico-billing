from pydantic import BaseModel


class TerminationGroupBalanceAdjustIn(BaseModel):
    amount_cents: int
    note: str = ""


def _column_names(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _group_payload(group):
    return {
        "id": group["id"],
        "name": group["name"],
        "balance_cents": int(group["balance_cents"] or 0),
    }


def ensure_schema(db):
    conn = db.get_conn()
    try:
        columns = _column_names(conn, "termination_groups")
        if "balance_cents" not in columns:
            conn.execute("ALTER TABLE termination_groups ADD COLUMN balance_cents INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE termination_groups SET balance_cents = 0 WHERE balance_cents IS NULL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS termination_group_balance_adjustments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id INTEGER NOT NULL, "
            "amount_cents INTEGER NOT NULL, "
            "balance_after_cents INTEGER NOT NULL, "
            "note TEXT NOT NULL DEFAULT '', "
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tg_balance_adjustments_group "
            "ON termination_group_balance_adjustments(group_id, created_at)"
        )
        conn.commit()
    finally:
        conn.close()


TERMINATOR_BALANCE_DASHBOARD_INJECTION = r"""
<style>
  .term-balance-actions { display:flex; gap:8px; justify-content:flex-end; align-items:center; flex-wrap:wrap; }
  .term-money-neg { color: var(--bad); }
</style>
<script>
(function () {
  if (window.__terminationGroupBalancePatch) return;
  window.__terminationGroupBalancePatch = true;
  let balanceGroupId = null;

  const safeMoney = (units, cur) => {
    try { return money(units, cur); }
    catch (_) { return ((Number(units) || 0) / 10000).toFixed(4) + (cur ? ' ' + cur : ''); }
  };
  const moneyInputValue = units => ((Number(units) || 0) / MONEY_SCALE).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') || '0';
  const signedMoneyClass = units => Number(units || 0) < 0 ? 'term-money-neg' : '';
  const supplierCurrency = () => {
    try { return (Object.values(clientMap || {})[0] || {}).currency || 'USD'; }
    catch (_) { return 'USD'; }
  };
  const groups = () => {
    try { return Array.isArray(groupList) ? groupList : Object.values(groupMap || {}); }
    catch (_) { return []; }
  };
  const parseSignedMoneyUnits = (value, label) => {
    const s = String(value ?? '').trim().replace(',', '.');
    if (!s) return 0;
    const sign = s.startsWith('-') ? -1 : 1;
    const raw = s.replace(/^[+-]/, '');
    return sign * parseMoneyUnits(raw, label);
  };

  function ensureTermBalanceDialog() {
    if (!document.getElementById('term-balance-dlg')) {
      document.body.insertAdjacentHTML('beforeend', `
        <dialog id="term-balance-dlg">
          <h3>Баланс терминатора</h3>
          <form id="term-balance-form">
            <label>Терминатор / аккаунт</label><input id="tb-name" disabled>
            <label>Текущий баланс</label><input id="tb-current" disabled>
            <label>Сумма (+ пополнить, - коррекция)</label><input id="tb-amount" type="number" step="0.0001" required autofocus>
            <label>Заметка</label><input id="tb-note" placeholder="topup, correction">
            <div class="row"><button type="button" class="ghost" onclick="document.getElementById('term-balance-dlg').close()">Отмена</button><button type="submit">Сохранить</button></div>
          </form>
        </dialog>`);
      document.getElementById('term-balance-form').addEventListener('submit', async e => {
        e.preventDefault();
        if (!balanceGroupId) return;
        try {
          const amount = parseSignedMoneyUnits(document.getElementById('tb-amount').value, 'Сумма');
          if (!amount) throw new Error('Сумма не должна быть нулём');
          await api(`/api/termination-groups/${balanceGroupId}/balance-adjust`, 'POST', {
            amount_cents: amount,
            note: document.getElementById('tb-note').value || ''
          });
          document.getElementById('term-balance-dlg').close();
          balanceGroupId = null;
          await load(true);
        } catch (err) { alert('Ошибка: ' + err.message); }
      });
    }

    const activeInput = document.getElementById('gr-active');
    if (activeInput && !document.getElementById('gr-balance')) {
      const label = activeInput.closest('label') || activeInput;
      label.insertAdjacentHTML('beforebegin',
        '<label>Стартовый баланс терминатора</label><input id="gr-balance" type="number" step="0.0001" min="0" value="0">');
    }
  }

  function hookGroupCreate() {
    const form = document.getElementById('group-form');
    if (!form || form.__termBalanceHooked) return;
    form.__termBalanceHooked = true;
    form.addEventListener('submit', async e => {
      const balanceInput = document.getElementById('gr-balance');
      let startBalance = 0;
      try { startBalance = balanceInput ? inputMoneyUnits('gr-balance', 'Стартовый баланс') : 0; }
      catch (err) {
        e.preventDefault();
        e.stopImmediatePropagation();
        alert('Ошибка: ' + err.message);
        return;
      }
      if (!startBalance) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      try {
        const created = await api('/api/termination-groups', 'POST', {
          name: document.getElementById('gr-name').value,
          ips: document.getElementById('gr-ips').value,
          gateway_name: document.getElementById('gr-gw').value || '',
          active: document.getElementById('gr-active').checked
        });
        await api(`/api/termination-groups/${created.id}/balance-adjust`, 'POST', {
          amount_cents: startBalance,
          note: 'initial balance'
        });
        groupDlg.close();
        e.target.reset();
        await load(true);
      } catch (err) { alert('Ошибка: ' + err.message); }
    }, true);
  }

  function renderTerminatorBalances() {
    const tbody = document.getElementById('t-groups');
    if (!tbody) return;
    const table = tbody.closest('table');
    if (table && !table.__termBalanceHeader) {
      const head = table.querySelector('thead tr');
      if (head) {
        head.innerHTML = '<th>Имя аккаунта</th><th>IP</th><th>FreeSWITCH gateway</th><th class="right">Баланс</th><th>Статус</th><th></th>';
        table.__termBalanceHeader = true;
      }
    }
    const cur = supplierCurrency();
    tbody.innerHTML = groups().map(g => {
      const name = window.editManageGroupName
        ? `<button class="manage-link-btn" onclick="editManageGroupName(${g.id})" title="Переименовать группу">${esc(g.name)}</button>`
        : esc(g.name);
      return `<tr>
        <td>${name}</td>
        <td class="mut">${esc(g.ips || '')}</td>
        <td>${g.gateway_name ? esc(g.gateway_name) : '<span class="mut">direct IP</span>'}</td>
        <td class="right ${signedMoneyClass(g.balance_cents)}">${safeMoney(g.balance_cents, cur)}</td>
        <td>${g.active ? '<span class="badge on">активна</span>' : '<span class="badge off">выкл</span>'}</td>
        <td class="right"><span class="term-balance-actions"><button class="small ghost" onclick="openTerminatorBalance(${g.id})">Баланс</button><button class="small danger" onclick="delGroup(${g.id})">✕</button></span></td>
      </tr>`;
    }).join('') || '<tr><td class="empty" colspan="6">Нет данных</td></tr>';
  }

  window.openTerminatorBalance = function (id) {
    ensureTermBalanceDialog();
    balanceGroupId = id;
    const group = groups().find(g => Number(g.id) === Number(id)) || {};
    const cur = supplierCurrency();
    document.getElementById('tb-name').value = group.name || ('#' + id);
    document.getElementById('tb-current').value = safeMoney(group.balance_cents || 0, cur);
    document.getElementById('tb-amount').value = '';
    document.getElementById('tb-note').value = '';
    document.getElementById('term-balance-dlg').showModal();
  };

  function installLoadHook() {
    try {
      if (typeof load !== 'function' || load.__termBalanceWrapped) return;
      const originalLoad = load;
      load = async function () {
        const result = await originalLoad.apply(this, arguments);
        ensureTermBalanceDialog();
        hookGroupCreate();
        renderTerminatorBalances();
        return result;
      };
      load.__termBalanceWrapped = true;
    } catch (_) {}
  }

  function boot() {
    ensureTermBalanceDialog();
    hookGroupCreate();
    installLoadHook();
    renderTerminatorBalances();
    setTimeout(renderTerminatorBalances, 500);
    setTimeout(() => { try { load(true); } catch (_) {} }, 1000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>
"""


def _install_dashboard_injection():
    import credit_limit_ui

    if "__terminationGroupBalancePatch" in credit_limit_ui.CREDIT_DASHBOARD_INJECTION:
        return
    credit_limit_ui.CREDIT_DASHBOARD_INJECTION += "\n" + TERMINATOR_BALANCE_DASHBOARD_INJECTION


def install(app, main, db):
    ensure_schema(db)
    _install_dashboard_injection()

    @app.on_event("startup")
    def _termination_group_balance_startup():
        ensure_schema(db)

    @app.post("/api/termination-groups/{gid}/balance-adjust", dependencies=getattr(main, "ADMIN_WRITE_AUTH", main.ADMIN_AUTH))
    def adjust_termination_group_balance(gid: int, data: TerminationGroupBalanceAdjustIn):
        amount = int(data.amount_cents or 0)
        if amount == 0:
            raise main.HTTPException(400, "Сумма не должна быть нулём")
        note = str(data.note or "").strip()[:300]
        ensure_schema(db)
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT * FROM termination_groups WHERE id = ?", (gid,)).fetchone()
            if group is None:
                conn.rollback()
                raise main.HTTPException(404, "Терминационная группа не найдена")
            old_balance = int(group["balance_cents"] or 0)
            new_balance = old_balance + amount
            conn.execute("UPDATE termination_groups SET balance_cents = ? WHERE id = ?", (new_balance, gid))
            conn.execute(
                "INSERT INTO termination_group_balance_adjustments "
                "(group_id, amount_cents, balance_after_cents, note) VALUES (?, ?, ?, ?)",
                (gid, amount, new_balance, note),
            )
            updated = conn.execute("SELECT * FROM termination_groups WHERE id = ?", (gid,)).fetchone()
            conn.commit()
            return {
                "ok": True,
                "termination_group": _group_payload(updated),
                "old_balance_cents": old_balance,
                "adjustment_cents": amount,
                "balance_cents": new_balance,
                "money_scale": db.MONEY_SCALE,
                "currency": "USD",
            }
        except main.HTTPException:
            conn.rollback()
            raise
        finally:
            conn.close()
