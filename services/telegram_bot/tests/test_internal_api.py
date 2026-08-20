"""Middleware-wiring smoke test for telegram_bot internal_api."""

import httpx
import pytest
from httpx import ASGITransport


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
    assert "SlowAPIASGIMiddleware" in middleware_class_names, (
        f"rate-limit middleware missing from internal_api; got {middleware_class_names}"
    )


async def test_health_returns_200_without_database_state() -> None:
    """The database-free adapter is ready without a PostgreSQL dependency."""
    from telegram_bot.internal_api import _internal_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_reload_nudges_endpoint_is_absent() -> None:
    """No general-key Research-to-Telegram mutation endpoint remains."""
    from telegram_bot.internal_api import _internal_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_internal_app), base_url="http://test"
    ) as c:
        resp = await c.post("/internal/reload-nudges")
    assert resp.status_code == 404


async def test_health_live_always_200() -> None:
    """GET /health/live remains an unconditional process liveness probe."""
    from telegram_bot.internal_api import _internal_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_main_calls_maybe_init_sentry_once_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """maybe_init_sentry runs exactly once at module import, mirroring the other services."""
    import sys
    from unittest.mock import MagicMock

    mock_init = MagicMock()
    monkeypatch.setattr("jarvis_common.sentry.maybe_init_sentry", mock_init)
    saved = sys.modules.pop("telegram_bot.main", None)
    try:
        import telegram_bot.main  # noqa: F401  # pyright: ignore[reportUnusedImport]

        mock_init.assert_called_once_with("telegram_bot")
    finally:
        if saved is not None:
            sys.modules["telegram_bot.main"] = saved
        else:
            sys.modules.pop("telegram_bot.main", None)
