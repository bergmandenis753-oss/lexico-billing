try:
    import main_compat  # noqa: F401
except Exception as exc:
    print(f"[billing compat warn] compatibility patch was not loaded: {exc}")
