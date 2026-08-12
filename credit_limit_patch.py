from fastapi import Request

from credit_limit_calls import install_call_routes
from credit_limit_clients import install_client_routes
from credit_limit_common import (
    available_client_units,
    client_credit_limit_units,
    ensure_schema,
    max_charge_units_for_client,
    minimum_client_balance_units,
    remove_routes,
)
from credit_limit_ui import dashboard_html


def install(app, main, db):
    db.client_credit_limit_units = client_credit_limit_units
    db.minimum_client_balance_units = minimum_client_balance_units
    db.available_client_units = available_client_units
    db.max_charge_units_for_client = max_charge_units_for_client
    ensure_schema(db)

    @app.on_event("startup")
    def _credit_limit_startup():
        ensure_schema(db)

    remove_routes(app, "/", {"GET"})
    remove_routes(app, "/api/clients", {"GET", "POST"})
    remove_routes(app, "/api/clients/{cid}", {"PATCH"})
    remove_routes(app, "/api/reserve", {"POST"})
    remove_routes(app, "/api/finalize", {"POST"})
    remove_routes(app, "/api/ops/client-balance-adjust", {"POST"})

    @app.get("/", dependencies=main.ADMIN_AUTH)
    def dashboard(request: Request):
        return dashboard_html()

    install_client_routes(app, main, db)
    install_call_routes(app, main, db)
