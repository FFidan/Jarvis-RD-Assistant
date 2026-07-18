"""Bootstrap setup-token gate.

The first-run setup wizard's WRITE endpoints must require a valid
``X-Setup-Token`` while no admin exists when a token is configured, while the
read-only probes (and the whole surface when no token is configured) stay open.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import paper_ingestion.routers.setup as setup_router
import pytest
from fastapi import HTTPException, Response
from jarvis_common.settings import get_secrets_settings
from jarvis_common.testing import make_pool_and_conn
from starlette.datastructures import Headers

_TOKEN = "test-sentinel-token"


@pytest.fixture(autouse=True)
def _disable_limiter():
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


@pytest.fixture
def _token_set(monkeypatch):
    monkeypatch.setenv("JARVIS_SETUP_TOKEN", _TOKEN)
    get_secrets_settings.cache_clear()
    yield
    get_secrets_settings.cache_clear()


@pytest.fixture
def _token_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_SETUP_TOKEN", raising=False)
    get_secrets_settings.cache_clear()
    yield
    get_secrets_settings.cache_clear()


def _bootstrap_request(*, method: str, token: str | None) -> SimpleNamespace:
    """A request against an install with zero admins (bootstrap mode)."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool, _ = make_pool_and_conn(conn=conn)
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    headers = Headers({} if token is None else {"x-setup-token": token})
    return SimpleNamespace(app=app, state=state, cookies={}, method=method, headers=headers)


@pytest.mark.asyncio
async def test_bootstrap_write_without_token_is_forbidden(_token_set) -> None:
    request = _bootstrap_request(method="POST", token=None)
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.require_unconfigured_or_admin(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_bootstrap_write_with_wrong_token_is_forbidden(_token_set) -> None:
    request = _bootstrap_request(method="POST", token="wrong-token")
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.require_unconfigured_or_admin(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_bootstrap_write_with_correct_token_is_allowed(_token_set) -> None:
    request = _bootstrap_request(method="POST", token=_TOKEN)
    assert await setup_router.require_unconfigured_or_admin(request) is None


@pytest.mark.asyncio
async def test_bootstrap_get_is_open_without_token(_token_set) -> None:
    """Read-only probes stay open even when the token is configured."""
    request = _bootstrap_request(method="GET", token=None)
    assert await setup_router.require_unconfigured_or_admin(request) is None


@pytest.mark.asyncio
async def test_bootstrap_write_refused_when_token_unset_in_production(
    _token_unset, monkeypatch
) -> None:
    """Production fails closed: no configured token → 403 naming the remedy."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    request = _bootstrap_request(method="POST", token=None)
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.require_unconfigured_or_admin(request)
    assert exc_info.value.status_code == 403
    assert "JARVIS_SETUP_TOKEN" in exc_info.value.detail
    assert "init-secrets" in exc_info.value.detail


@pytest.mark.asyncio
async def test_bootstrap_write_open_when_token_unset_in_development(
    _token_unset, monkeypatch, caplog
) -> None:
    """Non-production keeps the backward-compat warn+allow window (no-op gate)."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    request = _bootstrap_request(method="POST", token=None)
    with caplog.at_level(logging.WARNING, logger="paper_ingestion.routers.setup"):
        assert await setup_router.require_unconfigured_or_admin(request) is None
    assert any("unprotected" in r.message for r in caplog.records), (
        "expected an unprotected-first-admin WARNING when no setup token is configured"
    )


@pytest.mark.asyncio
async def test_bootstrap_write_with_token_does_not_warn(_token_set, caplog) -> None:
    """A configured token closes the window — the no-op warning must not fire."""
    request = _bootstrap_request(method="POST", token=_TOKEN)
    with caplog.at_level(logging.WARNING, logger="paper_ingestion.routers.setup"):
        assert await setup_router.require_unconfigured_or_admin(request) is None
    assert not any("unprotected" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_create_first_admin_rejects_missing_token_in_bootstrap(_token_set) -> None:
    """The first-admin endpoint is token-gated in bootstrap mode."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    # outer guard count, inner advisory-locked count — both 0 (bootstrap).
    conn.fetchval = AsyncMock(side_effect=[0, 0])
    pool, _ = make_pool_and_conn(conn=conn)
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    request = SimpleNamespace(app=app, state=state, cookies={}, method="POST", headers=Headers({}))
    body = setup_router.AdminBody(email="admin@example.com")

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.create_first_admin(body, request, Response())
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_create_first_admin_409_takes_precedence_over_token(_token_set) -> None:
    """An existing-admin probe gets the informative 409, not a token 403."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    # outer guard count, inner advisory-locked count — admin already exists.
    conn.fetchval = AsyncMock(side_effect=[1])
    pool, _ = make_pool_and_conn(conn=conn)
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    request = SimpleNamespace(app=app, state=state, cookies={}, method="POST", headers=Headers({}))
    body = setup_router.AdminBody(email="second-admin@example.com")

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.create_first_admin(body, request, Response())
    assert exc_info.value.status_code == 409
