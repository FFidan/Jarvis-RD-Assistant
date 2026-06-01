"""Tests for per-user Telegram pairing endpoints.

Covers:
- POST /api/telegram/pair-token  — auth required, issues 15-min token
- GET  /api/telegram/pairing     — returns status for current user
- DELETE /api/telegram/pairing   — removes pairing; auth required
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn


@pytest.fixture()
def app_fixture_with_user():
    """App with auth bypassed AND a fake authenticated user (user_id=7)."""
    from jarvis_common import current_user_id_or_none, verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    original_limiter_enabled = app.state.limiter.enabled
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # Inject a concrete user_id=7 so session-gated endpoints work.
    app.dependency_overrides[current_user_id_or_none] = lambda: 7

    yield app, conn

    app.state.limiter.enabled = original_limiter_enabled
    app.dependency_overrides.clear()


@pytest.fixture()
def app_fixture_no_user():
    """App with auth bypassed but NO session user (unauthenticated caller)."""
    from jarvis_common import current_user_id_or_none, verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    original_limiter_enabled = app.state.limiter.enabled
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_or_none] = lambda: None

    yield app, conn

    app.state.limiter.enabled = original_limiter_enabled
    app.dependency_overrides.clear()


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


@pytest.mark.asyncio
async def test_pair_token_deletes_previous_unconsumed(app_fixture_with_user):
    """Issuing a new token deletes any previous unconsumed tokens for the user."""
    app, conn = app_fixture_with_user
    conn.execute = AsyncMock(return_value="EXECUTE 1")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pair-token")

    assert resp.status_code == 200
    # First execute: DELETE unconsumed tokens; second: INSERT new token
    calls = conn.execute.await_args_list
    sql_stmts = [c.args[0] for c in calls]
    assert any(
        "DELETE FROM telegram_pairing_tokens" in s and "consumed_at IS NULL" in s for s in sql_stmts
    ), "Expected DELETE of previous unconsumed tokens"
    assert any("INSERT INTO telegram_pairing_tokens" in s for s in sql_stmts)


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
async def test_get_user_pairing_unauthenticated_returns_false(app_fixture_no_user):
    """Unauthenticated caller → paired=False (no 401, graceful)."""
    app, _conn = app_fixture_no_user

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/telegram/pairing")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is False


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


@pytest.mark.asyncio
async def test_remove_pairing_deletes_row_and_tokens(app_fixture_with_user):
    """DELETE removes telegram_user_pairings row and unconsumed tokens."""
    app, conn = app_fixture_with_user
    conn.execute = AsyncMock(return_value="DELETE 1")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/telegram/pairing")

    assert resp.status_code == 204
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert any("DELETE FROM telegram_user_pairings" in s for s in sqls)
    assert any(
        "DELETE FROM telegram_pairing_tokens" in s and "consumed_at IS NULL" in s for s in sqls
    )
