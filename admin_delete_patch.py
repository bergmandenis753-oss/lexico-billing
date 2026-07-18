from fastapi import HTTPException


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


def _ensure_client_deleted_at(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()]
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE clients ADD COLUMN deleted_at TEXT")
        conn.commit()


def _safe_archive_ip(raw_ip):
    return "".join("_" if ch in ",; \t\r\n" else ch for ch in str(raw_ip or ""))


def install(app, main, db):
    _remove_routes(app, "/api/clients/{cid}", {"DELETE"})

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
            archived_ip = f"deleted:{cid}:{db.now()}:{_safe_archive_ip(row['sip_ip'])}"
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
