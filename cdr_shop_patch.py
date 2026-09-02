from fastapi import HTTPException


def _rows(rows):
    return [dict(row) for row in rows]


def _route_exists(app, path, method):
    method = method.upper()
    for route in app.router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return True
    return False


def install(app, main, db):
    if _route_exists(app, "/api/ops/client-cdr-duration/{client_id}", "GET"):
        return

    @app.get("/api/ops/client-cdr-duration/{client_id}", dependencies=main.API_AUTH)
    def ops_client_cdr_duration(client_id: int, min_billsec: int = 0, limit: int = 50):
        db.init_db()
        min_billsec = max(0, int(min_billsec or 0))
        limit = min(200, max(1, int(limit or 50)))
        conn = db.get_conn()
        try:
            client = conn.execute("SELECT id, name, currency FROM clients WHERE id = ?", (client_id,)).fetchone()
            if client is None:
                raise HTTPException(404, "Клиент не найден")
            rows = conn.execute(
                """
                SELECT
                    cdr.*,
                    clients.name AS client_name,
                    clients.sip_ip AS client_sip_ip,
                    clients.currency AS client_currency
                FROM cdr
                LEFT JOIN clients ON clients.id = cdr.client_id
                WHERE cdr.client_id = ?
                  AND COALESCE(cdr.billsec, 0) > ?
                  AND date(cdr.started_at) = date('now')
                ORDER BY cdr.started_at DESC, cdr.id DESC
                LIMIT ?
                """,
                (client_id, min_billsec, limit),
            ).fetchall()
            return {
                "ok": True,
                "client_id": client_id,
                "client_name": client["name"],
                "client_currency": client["currency"] or "USD",
                "min_billsec": min_billsec,
                "limit": limit,
                "money_scale": db.MONEY_SCALE,
                "cdr": _rows(rows),
            }
        finally:
            conn.close()
