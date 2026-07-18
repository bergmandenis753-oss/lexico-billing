try:
    import main_compat  # noqa: F401
    try:
        import ai_diag
        ai_diag.install(main_compat.app, main_compat.main, main_compat.db)
    except Exception as exc:
        print(f"[billing ai diag warn] AI diagnostics were not loaded: {exc}")
except Exception as exc:
    print(f"[billing compat warn] compatibility patch was not loaded: {exc}")
