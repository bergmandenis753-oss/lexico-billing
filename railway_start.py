import os

import uvicorn


def _selected_app():
    role = (os.getenv("LEXICO_APP_ROLE") or "").strip().lower()
    if role in {"bot", "telegram", "telegram_bot"}:
        return "telegram_portal_entry:app"
    if role in {"web", "billing"}:
        return "ops_entry:app"

    service_name = (os.getenv("RAILWAY_SERVICE_NAME") or "").strip().lower()
    if "bot" in service_name:
        return "telegram_portal_entry:app"

    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("BILLING_API_BASE_URL"):
        return "telegram_portal_entry:app"

    return "ops_entry:app"


if __name__ == "__main__":
    port = int(os.getenv("PORT") or "8080")
    uvicorn.run(_selected_app(), host="0.0.0.0", port=port)
