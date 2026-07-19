"""Middleware-wiring smoke test for telegram_bot internal_api."""

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn


def _clear_sweep_memo(app) -> None:
    from jarvis_common.health import _SWEEP_MEMO_ATTR, _SWEEP_TASK_ATTR

    if hasattr(app.state, _SWEEP_MEMO_ATTR):
        delattr(app.state, _SWEEP_MEMO_ATTR)
    if hasattr(app.state, _SWEEP_TASK_ATTR):
        delattr(app.state, _SWEEP_TASK_ATTR)


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


async def test_health_returns_200_when_postgres_ok() -> None:
    """GET /health -> 200 {"status": "ok"} when the postgres probe succeeds."""
    from telegram_bot.internal_api import _internal_app

    pool, _conn = make_pool_and_conn(fetchval_return=1)
    _internal_app.state.db_pool = pool
    _clear_sweep_memo(_internal_app)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_health_returns_503_when_postgres_probe_fails() -> None:
    """GET /health -> 503 {"status": "degraded"} when the postgres probe fails.

    The decisive proof that /health is wired to a real probe: a 200-only
    happy-path test cannot distinguish a real check from a hardcoded "ok".
    """
    from telegram_bot.internal_api import _internal_app

    pool, _conn = make_pool_and_conn(raise_on_acquire=RuntimeError("DB down"))
    _internal_app.state.db_pool = pool
    _clear_sweep_memo(_internal_app)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded"}


async def test_health_live_always_200_even_when_postgres_is_down() -> None:
    """GET /health/live never runs the postgres probe — always 200 regardless."""
    from telegram_bot.internal_api import _internal_app

    pool, _conn = make_pool_and_conn(raise_on_acquire=RuntimeError("DB down"))
    _internal_app.state.db_pool = pool
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
