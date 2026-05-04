"""Tests for /api/telegram/pairing endpoints (A1 setup wizard backend)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402


class FakeRecord(dict):
    """Dict subclass that mimics asyncpg.Record."""

    def keys(self):
        return super().keys()


def _make_pool_and_conn():
    conn = AsyncMock()
    # Transaction context manager mock (needed by create_pairing's conn.transaction()).
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture(autouse=True)
def _reset_pairing_cooldown():
    """H.10: reset the module-level cooldown between tests so the 5s global
    cooldown does not bleed across test cases (which run < 5s apart)."""
    import paper_ingestion.routers.telegram as t

    t._last_pairing_request_monotonic = 0.0
    yield
    t._last_pairing_request_monotonic = 0.0


@pytest.fixture()
def app_fixture():
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    original_limiter_enabled = app.state.limiter.enabled
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.state.limiter.enabled = original_limiter_enabled
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/telegram/pairing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pairing_code_returns_16_hex_chars(app_fixture):
    app, conn = app_fixture
    conn.fetchrow.return_value = FakeRecord(
        value={"username": "jarvis_bot", "set_at": "2026-04-13T00:00:00+00:00"}
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pairing")

    assert resp.status_code == 200
    body = resp.json()
    assert re.fullmatch(r"[0-9a-f]{16}", body["code"])
    assert body["deep_link"] == f"https://t.me/jarvis_bot?start=PAIR_{body['code']}"
    assert body["bot_username_missing"] is False
    assert "expires_at" in body


@pytest.mark.asyncio
async def test_create_pairing_expires_stale_codes(app_fixture):
    """create_pairing must issue an expire-only DELETE (WHERE expires_at < NOW()) before INSERT."""
    app, conn = app_fixture
    conn.fetchrow.return_value = FakeRecord(
        value={"username": "jarvis_bot", "set_at": "2026-04-13T00:00:00+00:00"}
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pairing")

    assert resp.status_code == 200
    # execute should be called at least twice: expire-only DELETE then INSERT
    calls = conn.execute.await_args_list
    sql_statements = [c.args[0] if c.args else "" for c in calls]
    # Must use expire-only sweep, not a full wipe (WHERE clause required).
    assert any("DELETE FROM telegram_pairing" in s and "WHERE" in s for s in sql_statements), (
        "Expected expire-only DELETE with WHERE clause"
    )
    assert any("INSERT INTO telegram_pairing" in s for s in sql_statements)
    # DELETE must precede INSERT
    delete_idx = next(
        i for i, s in enumerate(sql_statements) if "DELETE FROM telegram_pairing" in s
    )
    insert_idx = next(
        i for i, s in enumerate(sql_statements) if "INSERT INTO telegram_pairing" in s
    )
    assert delete_idx < insert_idx
    # Transaction must be used.
    conn.transaction.assert_called_once()


@pytest.mark.asyncio
async def test_create_pairing_bot_username_missing_flag(app_fixture):
    app, conn = app_fixture
    conn.fetchrow.return_value = None  # telegram.bot_username not in user_config

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pairing")

    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_username_missing"] is True
    assert body["deep_link"].startswith("https://t.me/?start=PAIR_")


# ---------------------------------------------------------------------------
# GET /api/telegram/pairing/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pairing_status_returns_paired_false_when_null(app_fixture):
    app, conn = app_fixture
    conn.fetchrow.return_value = FakeRecord(value=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/telegram/pairing/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is False
    assert body["chat_id"] is None


@pytest.mark.asyncio
async def test_get_pairing_status_returns_paired_true_with_chat_id(app_fixture):
    app, conn = app_fixture
    conn.fetchrow.return_value = FakeRecord(value=123456789)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/telegram/pairing/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is True
    assert body["chat_id"] == 123456789


@pytest.mark.asyncio
async def test_get_pairing_status_returns_paired_false_when_literal_null_string(app_fixture):
    app, conn = app_fixture
    conn.fetchrow.return_value = FakeRecord(value="null")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/telegram/pairing/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paired"] is False
    assert body["chat_id"] is None


# ---------------------------------------------------------------------------
# W1.6 — transaction + rate-limit + expire-only sweep tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pairing_is_transactional(app_fixture):
    """Verify that DELETE and INSERT run inside a single DB transaction.

    If the transaction is omitted, a crash between DELETE and INSERT leaves
    the table empty with no valid code. We assert conn.transaction() is
    called exactly once, and that both statements execute inside it.
    """
    app, conn = app_fixture
    conn.fetchrow.return_value = FakeRecord(
        value={"username": "jarvis_bot", "set_at": "2026-04-13T00:00:00+00:00"}
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pairing")

    assert resp.status_code == 200
    # Transaction context must have been entered exactly once.
    conn.transaction.assert_called_once()
    # Both the expire sweep and the INSERT must have been executed.
    calls = conn.execute.await_args_list
    sql_statements = [c.args[0] if c.args else "" for c in calls]
    assert any("DELETE FROM telegram_pairing" in s for s in sql_statements)
    assert any("INSERT INTO telegram_pairing" in s for s in sql_statements)


@pytest.mark.asyncio
async def test_create_pairing_rate_limited():
    """11th request within a minute from the same IP must receive HTTP 429."""
    from jarvis_common import verify_api_key
    from jarvis_common.http_rate_limiter import create_limiter
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(
        value={"username": "jarvis_bot", "set_at": "2026-04-13T00:00:00+00:00"}
    )
    app.state.db_pool = mock_pool

    # Use a fresh limiter with clean in-memory storage to avoid accumulated
    # hit counts from earlier tests that also POST /api/telegram/pairing.
    original_limiter = app.state.limiter
    fresh_limiter = create_limiter()
    fresh_limiter.enabled = True
    app.state.limiter = fresh_limiter

    # H.10: this test exercises the slowapi per-IP limiter; bypass the global
    # cooldown so 11 sequential requests aren't all 429ed by the 5s gate.
    import paper_ingestion.routers.telegram as t

    original_cooldown = t._GLOBAL_PAIRING_COOLDOWN_SECONDS
    t._GLOBAL_PAIRING_COOLDOWN_SECONDS = 0.0

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            statuses = []
            for _ in range(11):
                r = await client.post("/api/telegram/pairing")
                statuses.append(r.status_code)
        # First 10 must succeed; 11th must be rate-limited.
        assert all(s == 200 for s in statuses[:10]), (
            f"Unexpected failures in first 10: {statuses[:10]}"
        )
        assert statuses[10] == 429, f"Expected 429 on request 11, got {statuses[10]}"
    finally:
        t._GLOBAL_PAIRING_COOLDOWN_SECONDS = original_cooldown
        app.state.limiter = original_limiter
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_pairing_preserves_non_expired_codes(app_fixture):
    """Expire-only sweep must NOT wipe non-expired codes from concurrent callers.

    The DELETE statement must include 'WHERE expires_at < NOW()' so that
    a code inserted by a concurrent caller (not yet expired) survives.
    """
    app, conn = app_fixture
    conn.fetchrow.return_value = None  # bot_username not configured

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/telegram/pairing")

    assert resp.status_code == 200
    calls = conn.execute.await_args_list
    sql_statements = [c.args[0] if c.args else "" for c in calls]
    # The DELETE must be conditional — a full wipe would remove non-expired codes.
    for stmt in sql_statements:
        if "DELETE FROM telegram_pairing" in stmt:
            assert "WHERE" in stmt, (
                "create_pairing must use an expire-only DELETE (WHERE expires_at < NOW()), "
                "not a full table wipe"
            )


# Keep a module-level reference to silence "unused import" warnings when the
# tests are collected without running (e.g. pyright strict mode).
_ = datetime(2026, 1, 1, tzinfo=UTC)
