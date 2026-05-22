"""Live-PG regression for RB-1: ON CONFLICT partial-index predicate fix.

Migration 086 creates a PARTIAL unique index on review_logs(user_id,
idempotency_key) WHERE idempotency_key IS NOT NULL.  PostgreSQL arbiter
inference for a partial index REQUIRES the predicate to appear verbatim in
the ON CONFLICT clause; without it PG raises 42P10 (ambiguous inference) on
every fresh insert — i.e. the common path, not the conflict path.

The mocked tests in test_review_sync.py assert only the SQL string and
therefore cannot catch 42P10 (the mock never runs Postgres).  This test
exercises the real path against a live PG 16 container with all migrations
applied.

Opt-in:  JARVIS_RUN_LIVE_PG=1 pytest -m live_pg services/learning_engine/tests/test_review_sync_live.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from learning_engine.models import Rating, ReviewSyncEvent, ReviewSyncRequest
from learning_engine.routers import review

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _event(key: str, *, card_id: int, rating: Rating = Rating.GOOD) -> ReviewSyncEvent:
    return ReviewSyncEvent(
        idempotency_key=key,
        card_id=card_id,
        rating=rating,
        reviewed_at=_now() - timedelta(hours=1),
        review_duration_ms=5000,
    )


async def _bootstrap(pool: asyncpg.Pool) -> None:
    """Apply init.sql then all migrations so migration 086 partial index exists."""
    from paper_ingestion.migrations_runner import run_migrations

    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))
    await run_migrations(pool)


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register the same JSON/JSONB codec the production pool uses."""
    import json

    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def _seed_user_and_card(conn: asyncpg.pool.PoolConnectionProxy) -> tuple[int, int]:
    """Insert a user + card; return (user_id, card_id)."""
    email = f"rb1-live-{uuid.uuid4().hex[:8]}@example.com"
    user_id: int = await conn.fetchval("INSERT INTO users (email) VALUES ($1) RETURNING id", email)
    card_id: int = await conn.fetchval(
        """
        INSERT INTO cards (card_type, front, back, user_id)
        VALUES ('concept', 'Q', 'A', $1)
        RETURNING id
        """,
        user_id,
    )
    return user_id, card_id


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_review_sync_fresh_insert_no_42p10(live_pg_dsn: str) -> None:
    """Fresh-insert path of sync_reviews must not raise 42P10.

    Pre-fix the ON CONFLICT clause lacked the WHERE predicate required by the
    partial index, causing PostgreSQL to raise 42P10 on every first-time sync.

    Asserts:
    - A brand-new idempotency_key inserts cleanly (synced=1, skipped=0).
    - The review_logs row is durable (SELECT confirms presence).
    - The cards.fsrs_state was advanced (UPDATE applied).
    - Replaying the same key is idempotent: synced=1, skipped=0, no duplicate
      review_logs row (the ON CONFLICT DO NOTHING path).
    """
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=3, init=_init_conn)
    try:
        await _bootstrap(pool)

        async with pool.acquire() as conn:
            user_id, card_id = await _seed_user_and_card(conn)

        idempotency_key = f"rb1-{uuid.uuid4()}"

        # --- First sync: fresh insert ---
        resp = await review.sync_reviews.__wrapped__(
            SimpleNamespace(state=SimpleNamespace(user_id=user_id)),
            body=ReviewSyncRequest(reviews=[_event(idempotency_key, card_id=card_id)]),
            db_pool=pool,
            user_id=user_id,
        )
        assert resp.synced == 1, f"Expected synced=1, got {resp}"
        assert resp.skipped == 0, f"Expected skipped=0, got {resp}"

        # Verify the review_logs row was written.
        async with pool.acquire() as conn:
            log_row = await conn.fetchrow(
                "SELECT id FROM review_logs WHERE user_id=$1 AND idempotency_key=$2",
                user_id,
                idempotency_key,
            )
            assert log_row is not None, "review_logs row missing after fresh sync"

            # cards.fsrs_state must have been updated (INSERT won → card UPDATE ran).
            card_row = await conn.fetchrow(
                "SELECT fsrs_state, updated_at FROM cards WHERE id=$1", card_id
            )
            assert card_row is not None
            assert card_row["fsrs_state"] is not None

        # --- Replay: idempotent duplicate ---
        resp2 = await review.sync_reviews.__wrapped__(
            SimpleNamespace(state=SimpleNamespace(user_id=user_id)),
            body=ReviewSyncRequest(reviews=[_event(idempotency_key, card_id=card_id)]),
            db_pool=pool,
            user_id=user_id,
        )
        # The key was already seen in the pre-batch SELECT → synced (not skipped).
        assert resp2.synced == 1, f"Replay: expected synced=1, got {resp2}"
        assert resp2.skipped == 0, f"Replay: expected skipped=0, got {resp2}"

        # Only one review_logs row (no double-insert).
        async with pool.acquire() as conn:
            count: int = await conn.fetchval(
                "SELECT COUNT(*) FROM review_logs WHERE user_id=$1 AND idempotency_key=$2",
                user_id,
                idempotency_key,
            )
            assert count == 1, f"Expected exactly 1 review_logs row, found {count}"
    finally:
        await pool.close()
