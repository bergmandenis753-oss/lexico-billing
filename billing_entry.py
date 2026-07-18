from pydantic import BaseModel

import main_compat
import telegram_bot


app = main_compat.app
main = main_compat.main
db = main_compat.db


class PcapEventIn(BaseModel):
    observed_at: str = ""
    direction: str = ""
    src_ip: str = ""
    src_port: str = ""
    dst_ip: str = ""
    dst_port: str = ""
    method: str = ""
    status_code: int | None = None
    status_text: str = ""
    call_id: str = ""
    cseq: str = ""
    from_user: str = ""
    to_user: str = ""
    request_uri: str = ""
    user_agent: str = ""
    reason: str = ""
    raw_summary: str = ""


class PcapEventsIn(BaseModel):
    events: list[PcapEventIn]


PCAP_COLUMNS = (
    "observed_at",
    "direction",
    "src_ip",
    "src_port",
    "dst_ip",
    "dst_port",
    "method",
    "status_code",
    "status_text",
    "call_id",
    "cseq",
    "from_user",
    "to_user",
    "request_uri",
    "user_agent",
    "reason",
    "raw_summary",
)


def _trim(value, limit=300):
    return str(value or "")[:limit]


@app.on_event("startup")
def _billing_entry_startup():
    db.init_db()


@app.post("/api/pcap-events", dependencies=main.API_AUTH)
def ingest_pcap_events(data: PcapEventsIn):
    rows = []
    for event in data.events[:200]:
        item = event.model_dump() if hasattr(event, "model_dump") else event.dict()
        row = {}
        for key in PCAP_COLUMNS:
            if key == "status_code":
                row[key] = int(item[key]) if item.get(key) is not None else None
            elif key == "raw_summary":
                row[key] = _trim(item.get(key), 5000)
            else:
                row[key] = _trim(item.get(key), 300)
        rows.append(row)

    if not rows:
        return {"ok": True, "saved": 0}

    conn = db.get_conn()
    try:
        placeholders = ", ".join("?" for _ in PCAP_COLUMNS)
        conn.executemany(
            f"INSERT INTO pcap_events ({', '.join(PCAP_COLUMNS)}) VALUES ({placeholders})",
            [[row[column] for column in PCAP_COLUMNS] for row in rows],
        )
        conn.commit()
        return {"ok": True, "saved": len(rows)}
    finally:
        conn.close()


class _OpsRequest:
    headers = {"x-money-scale": str(db.MONEY_SCALE)}


@app.get("/api/ops/dashboard-data", dependencies=main.API_AUTH)
def ops_dashboard_data():
    return main_compat.dashboard_data(_OpsRequest())


telegram_bot.install(app, main, db)
