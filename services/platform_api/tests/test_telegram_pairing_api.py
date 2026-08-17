"""Platform contracts for per-user Telegram pairing endpoints.

Covers:
- POST /api/telegram/pair-token  — auth required, issues 15-min token
- GET  /api/telegram/pairing     — returns status for current user
- DELETE /api/telegram/pairing   — removes pairing; auth required
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from jarvis_common import current_user_id_strict
from jarvis_common.testing import FakeRecord, make_pool_and_conn
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
from platform_api.deps import get_db_pool, limiter
from platform_api.routers import telegram


@contextmanager
def _wired_app(user_id: int | None):
    """Wire the app with auth bypassed and *user_id* as the session identity."""
    app = FastAPI()
    app.include_router(telegram.router)
    mock_pool, conn = make_pool_and_conn()
    dependency_overrides = {}
    if user_id is not None:
        dependency_overrides[current_user_id_strict] = lambda: user_id
    with patch_pi_test_app(
        mock_pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides=dependency_overrides,
        ),
    ):
        yield app, conn


@pytest.fixture()
def app_fixture_with_user():
    """App with auth bypassed AND a fake authenticated user (user_id=7)."""
    with _wired_app(7) as wired:
        yield wired


@pytest.fixture()
def app_fixture_no_user():
    """App with auth bypassed but NO session user (unauthenticated caller)."""
    with _wired_app(None) as wired:
        yield wired


# ---------------------------------------------------------------------------
# POST /api/telegram/pair-token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_token_requires_auth(app_fixture_no_user):
    """Unauthenticated caller gets 401."""
    app, _conn = app_fixture_no_user

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pair-token")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pair_token_issues_token_for_authenticated_user(app_fixture_with_user):
    """Authenticated user gets a token with 15-minute expiry."""
    app, conn = app_fixture_with_user
    conn.execute = AsyncMock(return_value="EXECUTE 0")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pair-token")

    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert len(body["token"]) == 32  # 16 bytes hex = 32 chars
    # expires_at should be roughly 15 minutes from now
    expires = datetime.fromisoformat(body["expires_at"])
    now = datetime.now(UTC)
    delta = expires - now
    assert timedelta(minutes=14) < delta < timedelta(minutes=16)


# ---------------------------------------------------------------------------
# GET /api/telegram/pairing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_pairing_no_pairing_returns_false(app_fixture_with_user):
    """No pairing row → paired=False."""
    app, conn = app_fixture_with_user
    conn.fetchrow = AsyncMock(return_value=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/telegram/pairing")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is False
    assert body["chat_id"] is None


@pytest.mark.asyncio
async def test_get_user_pairing_returns_pairing_data(app_fixture_with_user):
    """Existing pairing row → paired=True with chat_id and username."""
    app, conn = app_fixture_with_user
    paired_at = datetime.now(UTC)
    conn.fetchrow = AsyncMock(
        return_value=FakeRecord(
            chat_id=98765,
            telegram_username="alice",
            paired_at=paired_at,
        )
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/telegram/pairing")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is True
    assert body["chat_id"] == 98765
    assert body["telegram_username"] == "alice"


@pytest.mark.asyncio
async def test_get_user_pairing_requires_auth(app_fixture_no_user):
    """Unauthenticated callers cannot probe pairing state."""
    app, _conn = app_fixture_no_user

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/telegram/pairing")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/telegram/pairing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_pairing_requires_auth(app_fixture_no_user):
    """Unauthenticated DELETE /api/telegram/pairing → 401."""
    app, _conn = app_fixture_no_user

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/telegram/pairing")

    assert resp.status_code == 401
