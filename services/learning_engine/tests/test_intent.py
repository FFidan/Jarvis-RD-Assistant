"""Tests for Today's Intent endpoints.

Covers:
- GET  /api/executive/intent/today  — returns None when no intent set
- POST /api/executive/intent/today  — upserts intent text
- GET  after POST  — returns the upserted value
- POST with empty string — clears intent (DELETE)
- POST with intent > 280 chars — 422 (Pydantic max_length)
- GET without X-API-Key — 401 (no key configured, no DEV_MODE)
- [live_pg] Migration 060: daily_intent.user_id is INTEGER NULL post-migration
"""

from __future__ import annotations

import datetime

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def intent_app():
    """Minimal app with mocked DB and disabled auth + rate limiting."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from learning_engine.deps import get_db_pool
    from learning_engine.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_strict] = lambda: 1

    yield app, conn

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_intent_today_empty(intent_app):
    """GET /api/executive/intent/today returns {intent: None, updated_at: None} when empty."""
    app, conn = intent_app
    conn.fetchrow.return_value = None  # no row for today

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/intent/today")

    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] is None
    assert data["updated_at"] is None


@pytest.mark.asyncio
async def test_post_intent_today_upsert(intent_app):
    """POST /api/executive/intent/today with text returns intent and populated updated_at."""
    app, conn = intent_app
    now = datetime.datetime(2026, 5, 6, 9, 0, 0, tzinfo=datetime.UTC)
    conn.fetchrow.return_value = FakeRecord(intent_text="hello", updated_at=now)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/intent/today",
            json={"intent": "hello"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "hello"
    assert data["updated_at"] is not None


@pytest.mark.asyncio
async def test_get_after_post_returns_same_intent(intent_app):
    """After a POST, GET returns the same intent value."""
    app, conn = intent_app
    now = datetime.datetime(2026, 5, 6, 10, 0, 0, tzinfo=datetime.UTC)
    # POST: upsert returns the new row
    conn.fetchrow.return_value = FakeRecord(intent_text="focus on RAG", updated_at=now)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        post_resp = await client.post(
            "/api/executive/intent/today",
            json={"intent": "focus on RAG"},
        )
        assert post_resp.status_code == 200

        # Now configure GET to return the same row
        conn.fetchrow.return_value = FakeRecord(intent_text="focus on RAG", updated_at=now)
        get_resp = await client.get("/api/executive/intent/today")

    assert get_resp.status_code == 200
    assert get_resp.json()["intent"] == "focus on RAG"


@pytest.mark.asyncio
async def test_post_empty_string_clears_intent(intent_app):
    """POST with empty intent calls DELETE and returns {intent: None, updated_at: None}."""
    app, conn = intent_app
    conn.execute.return_value = "DELETE 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/intent/today",
            json={"intent": ""},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] is None
    assert data["updated_at"] is None

    # Verify DELETE was called (execute called, not fetchrow)
    conn.execute.assert_called_once()
    delete_sql = conn.execute.call_args[0][0]
    assert "DELETE" in delete_sql.upper()
    assert "daily_intent" in delete_sql.lower()


@pytest.mark.asyncio
async def test_post_empty_string_then_get_returns_none(intent_app):
    """After POST with empty string, GET also returns Nones (DELETE happened)."""
    app, conn = intent_app
    conn.execute.return_value = "DELETE 1"
    conn.fetchrow.return_value = None  # nothing in DB after delete

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        post_resp = await client.post(
            "/api/executive/intent/today",
            json={"intent": "   "},  # whitespace-only also triggers delete
        )
        assert post_resp.status_code == 200
        assert post_resp.json()["intent"] is None

        get_resp = await client.get("/api/executive/intent/today")
        assert get_resp.status_code == 200
        assert get_resp.json()["intent"] is None


@pytest.mark.asyncio
async def test_post_intent_too_long_returns_422(intent_app):
    """POST with intent longer than 280 chars returns 422 (Pydantic max_length)."""
    app, _ = intent_app
    long_intent = "x" * 281

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/intent/today",
            json={"intent": long_intent},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_intent_without_api_key_returns_401(monkeypatch):
    """GET /api/executive/intent/today without X-API-Key returns 401.

    verify_api_key raises HTTPException(401) when no key is configured and
    DEV_MODE is not set.  We ensure no dependency overrides bypass auth here.
    """
    from jarvis_common.auth import refresh_api_key_cache
    from learning_engine.main import app

    # Ensure no API key is set and DEV_MODE is off
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.delenv("DEV_MODE", raising=False)
    refresh_api_key_cache()

    # Clear any overrides left by other fixtures (safety net)
    app.dependency_overrides.clear()

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/executive/intent/today")

        assert resp.status_code == 401
    finally:
        # Restore override-free state — other tests may run after this
        app.dependency_overrides.clear()
        refresh_api_key_cache()


# ---------------------------------------------------------------------------
# Live-PG migration test (gated by JARVIS_RUN_LIVE_PG=1)
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_060_daily_intent_user_id_is_integer(live_pg_dsn: str) -> None:
    """Migration 060 changes daily_intent.user_id from TEXT NOT NULL to INTEGER NULL.

    Verifies:
    - The column data_type is 'integer' post-migration.
    - The column is_nullable is 'YES'.
    - The NULLS NOT DISTINCT unique index on (user_id, intent_date) exists.
    - An upsert with user_id=NULL round-trips correctly.
    """
    from pathlib import Path

    import asyncpg
    from paper_ingestion.migrations_runner import run_migrations

    repo_root = Path(__file__).resolve().parents[3]
    init_sql = repo_root / "db" / "init.sql"

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(init_sql.read_text(encoding="utf-8"))
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # Verify column type and nullability
            row = await conn.fetchrow(
                """
                SELECT data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'daily_intent' AND column_name = 'user_id'
                """,
            )
        assert row is not None, "daily_intent.user_id column not found"
        assert row["data_type"] == "integer", f"Expected INTEGER, got {row['data_type']!r}"
        assert row["is_nullable"] == "YES", (
            f"Expected nullable, got is_nullable={row['is_nullable']!r}"
        )

        async with pool.acquire() as conn:
            # Verify the NULLS NOT DISTINCT unique index exists
            idx_row = await conn.fetchrow(
                """
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'daily_intent'
                  AND indexname = 'daily_intent_user_date_uniq'
                """,
            )
        assert idx_row is not None, "daily_intent_user_date_uniq index not found"

        async with pool.acquire() as conn:
            # Upsert with NULL user_id then read back
            await conn.execute(
                """
                INSERT INTO daily_intent (user_id, intent_date, intent_text, updated_at)
                VALUES (NULL, CURRENT_DATE, 'migration-060-test', NOW())
                ON CONFLICT (user_id, intent_date) DO UPDATE
                  SET intent_text = EXCLUDED.intent_text,
                      updated_at  = NOW()
                """,
            )
            result = await conn.fetchrow(
                "SELECT intent_text FROM daily_intent"
                " WHERE user_id IS NULL AND intent_date = CURRENT_DATE",
            )
        assert result is not None, "Upserted row not found"
        assert result["intent_text"] == "migration-060-test"
    finally:
        await pool.close()
