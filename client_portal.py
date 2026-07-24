import hashlib
import hmac
import os
from html import escape

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse


def install(app, main, db):
    def money(value, scale=None, currency="USD"):
        amount = int(value or 0) / int(scale or db.MONEY_SCALE)
        return f"{amount:.4f} {currency}"

    def safe(value):
        return escape(str(value or ""))

    def portal_secret():
        secret = (os.getenv("CLIENT_PORTAL_SECRET") or os.getenv("API_SECRET_KEY") or "").strip()
        if not secret:
            raise HTTPException(503, "CLIENT_PORTAL_SECRET/API_SECRET_KEY не задан")
        return secret

    def portal_token(client_id):
        msg = f"client-portal:{client_id}".encode("utf-8")
        return hmac.new(portal_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()

    def check_portal_token(client_id, token):
        expected = portal_token(client_id)
        if not token or not hmac.compare_digest(str(token), expected):
            raise HTTPException(404, "Кабинет не найден")

    def external_base_url(request: Request):
        configured = (os.getenv("PUBLIC_BILLING_BASE_URL") or os.getenv("BILLING_PUBLIC_URL") or "").strip()
        return configured.rstrip("/") if configured else str(request.base_url).rstrip("/")

    @app.get("/api/ops/client-portal-link/{client_id}", dependencies=main.API_AUTH)
    def client_portal_link(client_id: int, request: Request):
        conn = db.get_conn()
        try:
            client = conn.execute("SELECT id, name FROM clients WHERE id = ?", (client_id,)).fetchone()
            if client is None:
                raise HTTPException(404, "Клиент не найден")
            base_url = external_base_url(request)
            return {
                "ok": True,
                "client_id": client["id"],
                "client_name": client["name"],
                "url": f"{base_url}/client/{client_id}?token={portal_token(client_id)}",
            }
        finally:
            conn.close()

    @app.get("/client/{client_id}", response_class=HTMLResponse)
    def client_portal(client_id: int, token: str = ""):
        check_portal_token(client_id, token)
        conn = db.get_conn()
        try:
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            if client is None:
                raise HTTPException(404, "Кабинет не найден")
            cdr_rows = conn.execute(
                "SELECT * FROM cdr WHERE client_id = ? ORDER BY id DESC LIMIT 20",
                (client_id,),
            ).fetchall()
            last_10_sum = conn.execute(
                "SELECT COALESCE(SUM(charged_cents),0) AS charged "
                "FROM (SELECT charged_cents FROM cdr WHERE client_id = ? ORDER BY id DESC LIMIT 10)",
                (client_id,),
            ).fetchone()
            today_sum = conn.execute(
                "SELECT COALESCE(SUM(charged_cents),0) AS charged FROM cdr "
                "WHERE client_id = ? AND date(started_at)=date('now')",
                (client_id,),
            ).fetchone()
        finally:
            conn.close()

        currency = client["currency"] or "USD"
        rows_html = []
        for row in cdr_rows:
            result = row["result"] or row["bridge_hangup_cause"] or row["hangup_cause"] or "-"
            rows_html.append(
                "<tr>"
                f"<td>{safe(row['started_at'] or '-')}</td>"
                f"<td>{safe(row['clid'] or '-')}</td>"
                f"<td>{safe(row['destination'] or '-')}</td>"
                f"<td>{int(row['billsec'] or 0)}</td>"
                f"<td>{safe(money(row['charged_cents'], currency=currency))}</td>"
                f"<td>{safe(result)}</td>"
                "</tr>"
            )
        if not rows_html:
            rows_html.append('<tr><td colspan="6" class="empty">Звонков пока нет</td></tr>')

        html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">
  <title>SloTELL - кабинет клиента</title>
  <style>
    :root {{
      --bg: #0b111d;
      --surface: #151e2d;
      --surface-2: #1b2637;
      --line: #2a3850;
      --text: #e8eef8;
      --muted: #93a1b5;
      --accent: #3b82f6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100dvh;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      padding: 18px 20px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }}
    .brand {{ font-size: 18px; font-weight: 800; }}
    .muted {{ color: var(--muted); }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 26px 18px 44px; }}
    .cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-bottom: 26px; }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .label {{ color: var(--muted); text-transform: uppercase; font-size: 12px; font-weight: 800; letter-spacing: .06em; }}
    .value {{ margin-top: 10px; font-size: 28px; font-weight: 850; }}
    h1 {{ margin: 0 0 20px; font-size: 28px; }}
    h2 {{ margin: 0 0 12px; font-size: 20px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
    th, td {{ padding: 13px 14px; text-align: left; border-bottom: 1px solid var(--line); }}
    th {{ color: var(--muted); background: var(--surface-2); text-transform: uppercase; font-size: 12px; font-weight: 800; letter-spacing: .06em; }}
    tr:last-child td {{ border-bottom: 0; }}
    .empty {{ color: var(--muted); text-align: center; }}
    .refresh {{
      color: var(--text);
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 14px;
      white-space: nowrap;
    }}
    @media (max-width: 760px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      .cards {{ grid-template-columns: 1fr; }}
      .value {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <div class="brand">SloTELL</div>
      <div class="muted">Личный кабинет клиента</div>
    </div>
    <a class="refresh" href="">Обновить</a>
  </header>
  <main>
    <h1>{safe(client['name'])}</h1>
    <section class="cards">
      <div class="card">
        <div class="label">Баланс</div>
        <div class="value">{safe(money(client['balance_cents'], currency=currency))}</div>
      </div>
      <div class="card">
        <div class="label">Списано за последние 10</div>
        <div class="value">{safe(money(last_10_sum['charged'], currency=currency))}</div>
      </div>
      <div class="card">
        <div class="label">Списано сегодня</div>
        <div class="value">{safe(money(today_sum['charged'], currency=currency))}</div>
      </div>
    </section>
    <section class="card">
      <h2>Последние звонки</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Время</th>
              <th>CLID</th>
              <th>Номер</th>
              <th>Длит., с</th>
              <th>Списано</th>
              <th>Отбой</th>
            </tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""
        response = HTMLResponse(html)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response
