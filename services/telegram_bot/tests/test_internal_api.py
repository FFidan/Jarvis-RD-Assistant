"""Middleware-wiring smoke test for telegram_bot internal_api (W5-CF5)."""


def test_internal_api_import_does_not_pull_paper_ingestion() -> None:
    import sys

    saved_pi = {k: v for k, v in sys.modules.items() if k.startswith("paper_ingestion")}
    saved_tb = {k: v for k, v in sys.modules.items() if k == "telegram_bot.internal_api"}
    try:
        for mod in list(saved_pi):
            del sys.modules[mod]
        sys.modules.pop("telegram_bot.internal_api", None)
        import telegram_bot.internal_api  # noqa: F401  # pyright: ignore[reportUnusedImport]

        leaked = [m for m in sys.modules if m.startswith("paper_ingestion")]
        assert not leaked, f"telegram_bot.internal_api pulled paper_ingestion: {leaked}"
    finally:
        sys.modules.update(saved_pi)
        sys.modules.update(saved_tb)


def test_internal_api_has_session_and_slowapi_middleware() -> None:
    from telegram_bot.internal_api import _internal_app

    middleware_class_names = {m.cls.__name__ for m in _internal_app.user_middleware}

    assert "SessionMiddleware" in middleware_class_names, (
        f"SessionMiddleware missing from internal_api; got {middleware_class_names}"
    )
    assert "SlowAPIMiddleware" in middleware_class_names, (
        f"SlowAPIMiddleware missing from internal_api; got {middleware_class_names}"
    )
