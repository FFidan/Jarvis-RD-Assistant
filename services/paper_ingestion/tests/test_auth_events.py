"""Tests that verify_api_key emits auth events on failure.

Verifies that ``jarvis_common.auth.verify_api_key`` calls ``log_event``
with category='auth', message='invalid_api_key' when an invalid API key
is provided and a db_pool is reachable via request.app.state.db_pool.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from jarvis_common.auth import refresh_api_key_cache, verify_api_key


@pytest.fixture(autouse=True)
def _reset_api_key_cache():
    """Reset the module-level API key cache before and after each test.

    monkeypatch restores env vars but NOT the module-level _CACHED_API_KEY that was
    set by refresh_api_key_cache(). This fixture ensures we don't leak state between
    tests across the full test suite.
    """
    yield
    refresh_api_key_cache()


def _request_with_pool(path: str, pool: object, client_host: str | None = "127.0.0.1"):
    """Build a minimal request stub that has app.state.db_pool set."""
    client = SimpleNamespace(host=client_host) if client_host is not None else None
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        app=app,
        client=client,
    )


def _request_no_pool(path: str):
    """Build a minimal request stub with NO db_pool (simulates pre-lifespan state)."""
    state = SimpleNamespace()  # no db_pool attribute
    app = SimpleNamespace(state=state)
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        app=app,
        client=SimpleNamespace(host="10.0.0.1"),
    )


@pytest.mark.asyncio
async def test_verify_api_key_emits_event_on_failure(monkeypatch) -> None:
    """Invalid API key triggers log_event with category='auth', message='invalid_api_key'."""
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("DEV_MODE", "false")
    refresh_api_key_cache()

    fake_pool = object()
    request = _request_with_pool("/api/papers", fake_pool, client_host="192.168.1.5")
    log_event_mock = AsyncMock()

    with patch("jarvis_common.auth.log_event", new=log_event_mock):
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(request, api_key="wrong-key")

    assert exc_info.value.status_code == 403
    log_event_mock.assert_awaited_once()
    call_kwargs = log_event_mock.await_args.kwargs
    assert call_kwargs["category"] == "auth"
    assert call_kwargs["message"] == "invalid_api_key"
    assert call_kwargs["level"] == "warning"
    assert call_kwargs["source"] == "verify_api_key"
    assert call_kwargs["pool"] is fake_pool
    assert call_kwargs.get("context", {}).get("ip") == "192.168.1.5"


@pytest.mark.asyncio
async def test_verify_api_key_no_event_on_success(monkeypatch) -> None:
    """Valid API key does NOT emit a log_event (too noisy per-request)."""
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("DEV_MODE", "false")
    refresh_api_key_cache()

    fake_pool = object()
    request = _request_with_pool("/api/papers", fake_pool)
    log_event_mock = AsyncMock()

    with patch("jarvis_common.auth.log_event", new=log_event_mock):
        await verify_api_key(request, api_key="x" * 32)

    log_event_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_api_key_failure_without_pool_still_raises(monkeypatch) -> None:
    """When no db_pool is available, the 403 is still raised (event is best-effort)."""
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("DEV_MODE", "false")
    refresh_api_key_cache()

    request = _request_no_pool("/api/papers")
    log_event_mock = AsyncMock()

    with patch("jarvis_common.auth.log_event", new=log_event_mock):
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(request, api_key="wrong-key")

    assert exc_info.value.status_code == 403
    # log_event should NOT have been called since no pool was available
    log_event_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_api_key_event_log_failure_does_not_block_403(monkeypatch) -> None:
    """If log_event raises, the HTTPException is still propagated (non-fatal)."""
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("DEV_MODE", "false")
    refresh_api_key_cache()

    fake_pool = object()
    request = _request_with_pool("/api/papers", fake_pool)
    failing_log_event = AsyncMock(side_effect=RuntimeError("DB connection lost"))

    with patch("jarvis_common.auth.log_event", new=failing_log_event):
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(request, api_key="bad-key")

    assert exc_info.value.status_code == 403
