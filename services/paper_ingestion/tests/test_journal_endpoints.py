"""Unit tests for journal entry CRUD endpoints (Phase 2 My Day).

Uses the __wrapped__ pattern (same as test_star_zotero_push_trigger.py) to
call the endpoint functions directly, bypassing FastAPI routing and the
slowapi rate-limiter decorator.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.routers import my_day

# ---------------------------------------------------------------------------
# Helpers — mirror test_star_zotero_push_trigger.py pattern
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Return a (pool, conn) pair backed by AsyncMock."""
    conn = AsyncMock()
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


def _mock_request():
    return MagicMock()


# ---------------------------------------------------------------------------
# GET /api/my-day/journal — entry found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_journal_entry_found():
    """fetchrow returns a row → endpoint returns JournalEntryResponse."""
    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 1,
        "date": today,
        "prompts": {"first_move": "Write tests"},
        "created_at": now,
        "updated_at": now,
    }
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await my_day.get_journal_entry.__wrapped__(
            _mock_request(), date=str(today), db_pool=pool
        )
    assert result.id == 1
    assert result.prompts.first_move == "Write tests"
    assert result.date == today


# ---------------------------------------------------------------------------
# GET /api/my-day/journal — entry not found (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_journal_entry_not_found():
    """fetchrow returns None → endpoint raises HTTPException 404."""
    from fastapi import HTTPException

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await my_day.get_journal_entry.__wrapped__(
                _mock_request(), date="2026-05-01", db_pool=pool
            )
    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# B1/P1 regression: GET /api/my-day/journal must bind a date OBJECT, not str
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_journal_entry_binds_date_object_not_str():
    """B1/P1: routed GET must not 500; ``$2`` must be a ``datetime.date``.

    Root cause: ``date: str = Query(...)`` bound the raw query string as ``$2``
    against the Postgres ``DATE`` column ``journal_entries.date``. asyncpg
    encodes a DATE param via ``.toordinal()``; a ``str`` has none →
    ``DataError: 'str' object has no attribute 'toordinal'`` → unhandled 500.

    This exercises FastAPI's query-param coercion (via ``TestClient``), which
    the existing ``__wrapped__`` tests bypass. With the buggy ``str`` annotation
    FastAPI passes the string straight through and ``$2`` is a ``str``; with the
    ``datetime.date`` annotation FastAPI coerces it to a real ``date``.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from paper_ingestion.deps import get_db_pool, limiter

    pool, conn = _make_pool_and_conn()
    today = date(2026, 5, 16)
    now = datetime(2026, 5, 16, 12, 0, 0)
    conn.fetchrow.return_value = {
        "id": 1,
        "date": today,
        "prompts": {"first_move": "Ground in code"},
        "created_at": now,
        "updated_at": now,
    }

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False
    app.include_router(my_day.router)
    app.dependency_overrides[get_db_pool] = lambda: pool
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/my-day/journal?date=2026-05-16")
        assert resp.status_code == 200, resp.text
        assert resp.json()["date"] == "2026-05-16"

        _sql, *bound = conn.fetchrow.await_args.args
        # $1 = user_id, $2 = date
        date_param = bound[1]
        assert isinstance(date_param, date), (
            f"B1/P1 regression: $2 must be datetime.date for the DATE column, "
            f"got {type(date_param).__name__!r}: {date_param!r} — asyncpg cannot "
            f"encode a str DATE param (no .toordinal())"
        )
        assert date_param == today
    finally:
        app.dependency_overrides.clear()
        limiter.enabled = True


# ---------------------------------------------------------------------------
# POST /api/my-day/journal — creates / updates entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_journal_entry_creates():
    """fetchrow returns upserted row → endpoint returns JournalEntryResponse."""
    from paper_ingestion.models.journal import JournalEntryCreate, JournalPrompts

    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 1,
        "date": today,
        "prompts": {"first_move": "Ship it"},
        "created_at": now,
        "updated_at": now,
    }
    body = JournalEntryCreate(date=today, prompts=JournalPrompts(first_move="Ship it"))
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await my_day.upsert_journal_entry.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.prompts.first_move == "Ship it"
    assert result.id == 1
    conn.fetchrow.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /api/my-day/journal — empty prompts round-trips correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_journal_entry_empty_prompts():
    """Empty JournalPrompts serialises to {} and returns without error."""
    from paper_ingestion.models.journal import JournalEntryCreate, JournalPrompts

    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 2,
        "date": today,
        "prompts": {},
        "created_at": now,
        "updated_at": now,
    }
    body = JournalEntryCreate(date=today, prompts=JournalPrompts())
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await my_day.upsert_journal_entry.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.prompts.first_move is None
    assert result.prompts.worked is None
    assert result.prompts.blocked is None


# ---------------------------------------------------------------------------
# H2 regression: no double json.dumps in upsert_journal_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_journal_entry_passes_raw_dict_not_json_string():
    """H2 regression: upsert_journal_entry must NOT call json.dumps on prompts.

    Before the fix, line 89 called json.dumps(prompts_dict) before passing to
    asyncpg. Because init_pg_connection registers json.dumps as the JSONB
    encoder, the value was serialised twice: once manually, once by the codec.
    The DB would store a JSON string '"{...}"' instead of a JSONB object '{...}'.

    This test verifies the raw dict is bound to $3, not a pre-serialised string.
    """
    from paper_ingestion.models.journal import JournalEntryCreate, JournalPrompts

    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 3,
        "date": today,
        "prompts": {"first_move": "Regression guard"},
        "created_at": now,
        "updated_at": now,
    }
    body = JournalEntryCreate(date=today, prompts=JournalPrompts(first_move="Regression guard"))
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await my_day.upsert_journal_entry.__wrapped__(_mock_request(), body=body, db_pool=pool)

    _sql, *bound_params = conn.fetchrow.await_args.args
    # $3 is index 2 in bound_params (0-based: $1=user_id, $2=date, $3=prompts)
    prompts_param = bound_params[2]

    assert isinstance(prompts_param, dict), (
        f"H2 regression: expected dict bound for $3::jsonb, "
        f"got {type(prompts_param).__name__!r}: {prompts_param!r}"
    )
    assert prompts_param == {"first_move": "Regression guard"}
    assert prompts_param != json.dumps(  # nolint:jsonb-double-encode
        {"first_move": "Regression guard"}
    ), "H2 regression: upsert_journal_entry is double-encoding the JSONB value"


# ---------------------------------------------------------------------------
# Live round-trip test (requires TEST_DATABASE_URL)
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — skipping live DB round-trip test",
)
async def test_upsert_journal_entry_jsonb_round_trip_live_db():
    """H2 live round-trip: prompts stored as JSONB object, not double-encoded string.

    Requires TEST_DATABASE_URL pointing to a Postgres instance.
    Asserts:
    - jsonb_typeof(prompts) == 'object'  (not 'string')
    - prompts dict matches the input
    - JournalPrompts(**prompts) succeeds without TypeError
    """
    import asyncpg
    from jarvis_common import init_pg_connection
    from paper_ingestion.models.journal import JournalPrompts

    try:
        pool = await asyncpg.create_pool(
            _DB_URL,
            min_size=1,
            max_size=2,
            init=init_pg_connection,
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to test DB: {exc}")
        return

    test_date = "2099-01-01"  # far-future date avoids collision with real data
    try:
        async with pool.acquire() as conn:
            # Ensure table exists (idempotent)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS journal_entries (
                    id         SERIAL PRIMARY KEY,
                    user_id    INTEGER,
                    date       DATE NOT NULL DEFAULT CURRENT_DATE,
                    prompts    JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE NULLS NOT DISTINCT (user_id, date)
                )
            """)
            # Remove any stale row from a previous run
            await conn.execute(
                "DELETE FROM journal_entries WHERE user_id IS NULL AND date = $1",
                test_date,
            )

        # Call the endpoint with a real asyncpg pool
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from paper_ingestion.deps import get_db_pool
        from paper_ingestion.routers.my_day import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db_pool] = lambda: pool

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/my-day/journal",
                json={"date": test_date, "prompts": {"first_move": "live test"}},
            )
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["prompts"]["first_move"] == "live test"

        # Verify DB directly: jsonb_typeof must be 'object'
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT jsonb_typeof(prompts) AS jtype, prompts "
                "FROM journal_entries WHERE user_id IS NULL AND date = $1",
                test_date,
            )

        assert row is not None, "Row not found after upsert"
        assert row["jtype"] == "object", (
            f"H2 regression: expected jsonb_typeof='object', got {row['jtype']!r}. "
            "The value was double-encoded as a JSON string."
        )
        assert row["prompts"] == {"first_move": "live test"}, (
            f"prompts mismatch: {row['prompts']!r}"
        )

        # Also verify JournalPrompts(**prompts) does not raise TypeError
        jp = JournalPrompts(**(row["prompts"] or {}))
        assert jp.first_move == "live test"

    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM journal_entries WHERE user_id IS NULL AND date = $1",
                test_date,
            )
        await pool.close()
