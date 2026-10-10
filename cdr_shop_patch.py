import json
import re
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException


ISO3166_DATA_PATH = Path(__file__).with_name("data") / "iso3166_countries.json"
COUNTRY_NAME_ALIASES = {
    "AS": "American Samoa",
    "CD": "DR of Congo",
    "CF": "Central African Republic",
    "CG": "Congo Brazzaville",
    "CI": "Ivory Coast",
    "CZ": "Czech Republic",
    "FO": "Faroe Island",
    "GB": "UK",
    "GF": "French Guyana",
    "GN": "Guinea Republic",
    "KP": "North Korea",
    "KR": "South Korea",
    "LU": "Luxemburg",
    "MH": "Marshall Island",
    "MM": "Myanmar",
    "PS": "Palestine Authority",
    "TC": "Turks & Caicos Island",
    "VA": "Vatican",
    "VC": "St Vincent & the Grenadines",
    "VG": "British Virgin Islands",
    "WS": "Western Samoa",
}


def _rows(rows):
    return [dict(row) for row in rows]


def _route_exists(app, path, method):
    method = method.upper()
    for route in app.router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return True
    return False


@lru_cache(maxsize=1)
def _iso_country_names():
    if not ISO3166_DATA_PATH.exists():
        return {}
    with ISO3166_DATA_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _country_for_iso(db, conn, country_code):
    code = str(country_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise HTTPException(400, "Укажите двухбуквенный код страны, например PL или CZ")
    country_name = COUNTRY_NAME_ALIASES.get(code) or _iso_country_names().get(code)
    if not country_name:
        raise HTTPException(404, f"Код страны {code} не найден")
    country = db.get_e164_country(conn, country_name)
    if country is None:
        raise HTTPException(404, f"Для страны {code} пока нет E.164 справочника")
    return code, country


def _cdr_number(db, row):
    number = db.normalize_phone_number(row["dial_destination"])
    if number:
        return number
    number = db.normalize_phone_number(row["destination"])
    client_prefix = db.normalize_phone_number(row["client_tech_prefix"])
    if client_prefix and number.startswith(client_prefix):
        number = number[len(client_prefix):]
    return number


def _prefix_country_index(conn):
    rows = conn.execute("SELECT prefix, country FROM e164_prefixes").fetchall()
    index = {str(row["prefix"]): row["country"] for row in rows}
    max_length = max((len(prefix) for prefix in index), default=0)
    return index, max_length


def _country_for_number(number, prefix_index, max_length):
    for length in range(min(len(number), max_length), 0, -1):
        country = prefix_index.get(number[:length])
        if country:
            return country
    return ""


def install(app, main, db):
    if not _route_exists(app, "/api/ops/client-cdr-duration/{client_id}", "GET"):
        @app.get("/api/ops/client-cdr-duration/{client_id}", dependencies=main.API_AUTH)
        def ops_client_cdr_duration(client_id: int, min_billsec: int = 0, limit: int = 5000):
            db.init_db()
            min_billsec = max(0, int(min_billsec or 0))
            limit = min(10000, max(1, int(limit or 5000)))
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

    if not _route_exists(app, "/api/ops/cdr-duration-country/{country_code}", "GET"):
        @app.get("/api/ops/cdr-duration-country/{country_code}", dependencies=main.API_AUTH)
        def ops_country_cdr_duration(country_code: str, min_billsec: int = 0, limit: int = 5000):
            db.init_db()
            min_billsec = max(0, int(min_billsec or 0))
            limit = min(10000, max(1, int(limit or 5000)))
            conn = db.get_conn()
            try:
                code, country = _country_for_iso(db, conn, country_code)
                matches = []
                prefix_index, max_prefix_length = _prefix_country_index(conn)
                rows = conn.execute(
                    """
                    SELECT
                        cdr.*,
                        clients.name AS client_name,
                        clients.sip_ip AS client_sip_ip,
                        clients.currency AS client_currency
                    FROM cdr
                    LEFT JOIN clients ON clients.id = cdr.client_id
                    WHERE COALESCE(cdr.billsec, 0) > ?
                      AND date(cdr.started_at) = date('now')
                    ORDER BY cdr.started_at DESC, cdr.id DESC
                    """,
                    (min_billsec,),
                )
                for row in rows:
                    number = _cdr_number(db, row)
                    resolved_country = _country_for_number(number, prefix_index, max_prefix_length) if number else ""
                    if not db.direction_matches(resolved_country, country["country"]):
                        continue
                    item = dict(row)
                    item["dial_destination"] = number
                    matches.append(item)
                    if len(matches) >= limit:
                        break
                return {
                    "ok": True,
                    "country_code": code,
                    "country_name": country["country"],
                    "min_billsec": min_billsec,
                    "limit": limit,
                    "money_scale": db.MONEY_SCALE,
                    "cdr": matches,
                }
            finally:
                conn.close()
