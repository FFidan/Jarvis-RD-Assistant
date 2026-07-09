"""Middleware-wiring smoke test for telegram_bot internal_api."""


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


def test_internal_api_version_delegates_to_app_version() -> None:
    # Regression guard for the FastAPI default "0.1.0": the internal API must
    # report the shared app_version() like the other services, not the stale
    # framework default. (app_version()'s own resolution is covered in
    # libs/jarvis_common/tests/test_version.py.)
    from jarvis_common.version import app_version
    from telegram_bot.internal_api import _internal_app

    assert _internal_app.version == app_version()


def test_internal_api_has_session_and_slowapi_middleware() -> None:
    from telegram_bot.internal_api import _internal_app

    middleware_class_names = {m.cls.__name__ for m in _internal_app.user_middleware}

    assert "SessionMiddleware" in middleware_class_names, (
        f"SessionMiddleware missing from internal_api; got {middleware_class_names}"
    )
    assert "SlowAPIMiddleware" in middleware_class_names, (
        f"SlowAPIMiddleware missing from internal_api; got {middleware_class_names}"
    )
