import main_compat


try:
    import client_route_isolation_patch
    client_route_isolation_patch.install(main_compat.app, main_compat.main, main_compat.db)
except Exception as exc:
    print(f"[billing route isolation warn] patch was not loaded: {exc}")


try:
    import ai_diag
    try:
        import ai_diag_patch
        ai_diag_patch.apply(ai_diag)
    except Exception as exc:
        print(f"[billing ai diag warn] full SIP patch was not loaded: {exc}")
    ai_diag.install(main_compat.app, main_compat.main, main_compat.db)
except Exception as exc:
    print(f"[billing ai diag warn] AI diagnostics were not loaded: {exc}")


app = main_compat.app
