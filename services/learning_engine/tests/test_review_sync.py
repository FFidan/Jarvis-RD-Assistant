"""Pure unit tests for ReviewSyncResponse schema (CFG-SYNCSTATS-1),
_build_fsrs_manager_from_db single-step warning path (CFG-RECVAL-1),
and the skipped-card idempotency fix (M11d).

These tests assert only on schema and unit-level behaviour, with no
mock-units patching router internals. Shape: pure-unit (no DB, no HTTP).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from learning_engine.models import ReviewSyncRequest, ReviewSyncResponse


def test_sync_response_has_already_synced_field():
    """already_synced field must be present in ReviewSyncResponse schema."""
    assert "already_synced" in ReviewSyncResponse.model_fields


def test_already_synced_has_default_zero():
    """already_synced must default to 0 so existing callers need not pass it."""
    resp = ReviewSyncResponse(synced=3, skipped=1)
    assert resp.already_synced == 0


def test_already_synced_rejects_negative():
    """already_synced must reject negative values (ge=0 constraint)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewSyncResponse(synced=0, skipped=0, already_synced=-1)


def test_synced_and_already_synced_are_independent():
    """synced counts new writes; already_synced counts replays — distinct fields."""
    resp = ReviewSyncResponse(synced=5, skipped=2, already_synced=3)
    assert resp.synced == 5
    assert resp.skipped == 2
    assert resp.already_synced == 3


# --- _build_fsrs_manager_from_db single-step warning path (CFG-RECVAL-1) ---


@pytest.mark.asyncio
async def test_build_fsrs_manager_single_step_emits_warning(caplog):
    """A single-element learning_steps list must build FSRSManager AND emit a warning."""
    import json

    from learning_engine.routers.review import _build_fsrs_manager_from_db

    # Build a fake asyncpg conn that returns a 1-element learning_steps JSON.
    fake_row_steps = MagicMock()
    fake_row_steps.__getitem__ = MagicMock(
        side_effect=lambda k: "fsrs.learning_steps" if k == "key" else json.dumps([5])
    )
    fake_conn = MagicMock()
    fake_conn.fetch = AsyncMock(return_value=[fake_row_steps])

    with caplog.at_level(logging.WARNING, logger="learning_engine.routers.review"):
        manager = await _build_fsrs_manager_from_db(fake_conn, user_id=None)

    assert manager is not None
    assert any("1 element" in r.message or "element(s)" in r.message for r in caplog.records), (
        "Expected a warning about non-standard step count"
    )


# --- _to_utc helper (TZ-UTC-1) ---


def test_to_utc_naive_gets_utc_tzinfo():
    """A naive datetime must have tzinfo=UTC attached; the value must be unchanged."""
    from datetime import UTC, datetime

    from learning_engine.routers.review import _to_utc

    naive = datetime(2024, 3, 15, 10, 30, 0)
    result = _to_utc(naive)
    assert result.tzinfo is UTC
    assert result.replace(tzinfo=None) == naive


def test_to_utc_aware_returned_unchanged():
    """An already-aware datetime must be returned exactly as-is."""
    from datetime import UTC, datetime

    from learning_engine.routers.review import _to_utc

    aware = datetime(2024, 3, 15, 10, 30, 0, tzinfo=UTC)
    result = _to_utc(aware)
    assert result is aware


@pytest.mark.asyncio
async def test_build_fsrs_manager_two_steps_no_warning(caplog):
    """A standard 2-element list must build FSRSManager without any warning."""
    import json

    from learning_engine.routers.review import _build_fsrs_manager_from_db

    fake_row_steps = MagicMock()
    fake_row_steps.__getitem__ = MagicMock(
        side_effect=lambda k: "fsrs.learning_steps" if k == "key" else json.dumps([1, 10])
    )
    fake_conn = MagicMock()
    fake_conn.fetch = AsyncMock(return_value=[fake_row_steps])

    with caplog.at_level(logging.WARNING, logger="learning_engine.routers.review"):
        manager = await _build_fsrs_manager_from_db(fake_conn, user_id=None)

    assert manager is not None
    step_warnings = [
        r for r in caplog.records if "element" in r.message and "learning_steps" in r.message
    ]
    assert not step_warnings, "No warning expected for standard 2-step list"


# ---------------------------------------------------------------------------
# M11d — duplicate idempotency_key for a missing card (SKIP-DEDUP-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_key_for_missing_card_counts_as_skipped_then_already_synced():
    """Two events with the same idempotency_key for a nonexistent card:
    first → skipped=1, second → already_synced=1 (NOT double-skipped).

    Regression guard for M11d: before the fix, the skipped-card path did NOT
    add the key to ``applied``, so both occurrences incremented ``skipped``
    instead of the second being counted as ``already_synced``.

    Shape: pure-unit — mocked pool, no HTTP, no DB.
    """
    from jarvis_common.testing import make_pool_and_conn

    from learning_engine.routers.review import sync_reviews

    idem_key = "dup-skip-key-001"
    reviewed_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

    # Both events share the same idempotency_key; the card does not exist.
    body = ReviewSyncRequest(
        reviews=[
            {  # type: ignore[list-item]
                "idempotency_key": idem_key,
                "card_id": 99999,
                "rating": 3,
                "reviewed_at": reviewed_at.isoformat(),
            },
            {  # type: ignore[list-item]
                "idempotency_key": idem_key,
                "card_id": 99999,
                "rating": 3,
                "reviewed_at": reviewed_at.isoformat(),
            },
        ]
    )

    # Pre-flight fetch: returns [] (no pre-existing applied keys and no fsrs config).
    # Chunk fetchrow: returns None (card not found).
    pool, conn = make_pool_and_conn(fetch_return=[], fetchrow_return=None)

    # Unwrap @limiter.limit so we can call the handler directly without an HTTP request.
    handler = getattr(sync_reviews, "__wrapped__", sync_reviews)
    request_mock = MagicMock()

    result = await handler(
        request=request_mock,
        body=body,
        db_pool=pool,
        user_id=1,
    )

    assert isinstance(result, ReviewSyncResponse), f"Expected ReviewSyncResponse; got {result!r}"
    assert result.skipped == 1, (
        f"Expected skipped=1 (first occurrence); got skipped={result.skipped}. "
        "The second occurrence must NOT be double-counted as skipped."
    )
    assert result.already_synced == 1, (
        f"Expected already_synced=1 (second occurrence hits idempotency fast-path); "
        f"got already_synced={result.already_synced}."
    )
    assert result.synced == 0, (
        f"Expected synced=0 (no card exists to write); got synced={result.synced}."
    )
