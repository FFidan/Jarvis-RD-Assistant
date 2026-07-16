"""Pure unit tests for ReviewSyncResponse schema (CFG-SYNCSTATS-1),
_build_fsrs_manager_from_db single-step warning path (CFG-RECVAL-1),
the skipped-card idempotency fix (M11d), and FSRS event-time threading.

These tests assert only on schema and unit-level behaviour, with no
mock-units patching router internals. Shape: pure-unit (no DB, no HTTP).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
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


# ---------------------------------------------------------------------------
# FSRS event-time threading
# ---------------------------------------------------------------------------


def test_schedule_review_event_time_shifts_next_due():
    """Passing review_datetime anchors FSRS intervals to the event time, not now.

    A review timestamped 48 hours in the past must produce a next_due that is
    ~48 hours earlier than scheduling without review_datetime (which anchors to
    the current clock).
    """
    from fsrs import Card

    from learning_engine.fsrs_manager import FSRSManager

    manager = FSRSManager()
    card_state = dict(Card().to_dict())
    past_time = datetime.now(UTC) - timedelta(hours=48)

    _, _, due_event_time = manager.schedule_review(card_state, rating=3, review_datetime=past_time)
    _, _, due_sync_time = manager.schedule_review(card_state, rating=3)

    assert due_event_time != due_sync_time, (
        "schedule_review with review_datetime must anchor next_due to the event time; "
        "got identical next_due for a 48-hour-old event vs. sync-time scheduling"
    )
    # Event-time next_due must precede sync-time next_due (interval starts earlier).
    assert due_event_time < due_sync_time, (
        f"Event-time next_due ({due_event_time}) must be earlier than sync-time "
        f"next_due ({due_sync_time}) for a 48-hour-delayed review"
    )


def test_schedule_review_no_review_datetime_anchors_to_now():
    """Regression: omitting review_datetime anchors next_due to the current clock.

    Without the fix (drop review_datetime arg), due_at is relative to now, not
    to the original review time.  This test captures the pre-fix baseline so that
    reverting the sync_reviews change causes test_schedule_review_event_time_shifts_next_due
    to collapse (both calls would then produce sync-time-relative results).
    """
    from fsrs import Card

    from learning_engine.fsrs_manager import FSRSManager

    manager = FSRSManager()
    card_state = dict(Card().to_dict())

    _, _, due = manager.schedule_review(card_state, rating=3)
    now = datetime.now(UTC)

    # Without review_datetime, the interval is anchored to "now", so due > now.
    assert due > now - timedelta(minutes=1), (
        "Without review_datetime, next_due must be relative to the current time"
    )


# ---------------------------------------------------------------------------
# Atomic daily_log write inside the per-event transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_today_event_writes_daily_log_inside_per_event_transaction():
    """A today-dated synced event increments daily_log inside its per-event
    transaction — NOT in a separate trailing connection.

    Pre-fix the handler did one extra pool.acquire() after the loop to apply a
    batched daily_log UPSERT; a crash before it (or an already-synced retry)
    permanently lost the count. The atomic write keeps acquires to: pre-flight (1)
    + one chunk (1) = 2.

    Shape: pure-unit — mocked pool, no HTTP, no DB.
    """
    from jarvis_common.testing import make_pool_and_conn

    from learning_engine.routers.review import sync_reviews

    body = ReviewSyncRequest(
        reviews=[
            {  # type: ignore[list-item]
                "idempotency_key": "today-atomic-1",
                "card_id": 1,
                "rating": 3,
                "reviewed_at": datetime.now(UTC).isoformat(),
            }
        ]
    )
    # fetch -> [] (no applied keys, no fsrs config rows); fetchrow -> owned card.
    pool, conn = make_pool_and_conn(
        fetch_return=[],
        fetchrow_return={"fsrs_state": {}},
    )
    # fetchval is called twice per event: the chronology-guard MAX(reviewed_at)
    # probe (no prior review -> None) then the log INSERT RETURNING id (123 = won).
    conn.fetchval = AsyncMock(
        side_effect=lambda q, *a, **k: None if "MAX(reviewed_at)" in q else 123
    )
    handler = getattr(sync_reviews, "__wrapped__", sync_reviews)

    result = await handler(request=MagicMock(), body=body, db_pool=pool, user_id=1)

    assert result.synced == 1, f"expected synced=1; got {result!r}"
    assert pool.acquire.call_count == 2, (
        "daily_log must be written inside the per-event transaction; a trailing "
        f"acquire signals a non-atomic batched update (got {pool.acquire.call_count})"
    )
    daily_log_writes = [c for c in conn.execute.await_args_list if "daily_log" in c.args[0]]
    assert len(daily_log_writes) == 1, (
        f"expected exactly one daily_log upsert; got {len(daily_log_writes)}"
    )


@pytest.mark.asyncio
async def test_sync_replays_events_oldest_first(monkeypatch):
    """Out-of-order events are replayed by ascending reviewed_at (oldest-first),
    so per-card FSRS state advances chronologically.

    Shape: pure-unit — mocked pool, FSRS scheduling intercepted to record order.
    """
    from jarvis_common.testing import make_pool_and_conn

    import learning_engine.routers.review as review_mod
    from learning_engine.routers.review import sync_reviews

    older = datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC)
    newer = datetime(2024, 6, 1, 20, 0, 0, tzinfo=UTC)
    # Submitted NEWEST-first (violating the contract) to prove server-side sort.
    body = ReviewSyncRequest(
        reviews=[
            {  # type: ignore[list-item]
                "idempotency_key": "ord-newer",
                "card_id": 1,
                "rating": 3,
                "reviewed_at": newer.isoformat(),
            },
            {  # type: ignore[list-item]
                "idempotency_key": "ord-older",
                "card_id": 1,
                "rating": 3,
                "reviewed_at": older.isoformat(),
            },
        ]
    )

    seen: list[datetime] = []

    class _RecordingManager:
        def schedule_review(self, state, rating, review_datetime=None):
            seen.append(review_datetime)
            return ({"s": 1}, {"l": 1}, datetime.now(UTC))

    async def _fake_build(conn, user_id=None):
        return _RecordingManager()

    monkeypatch.setattr(review_mod, "_build_fsrs_manager_from_db", _fake_build)

    pool, conn = make_pool_and_conn(
        fetch_return=[],
        fetchrow_return={"fsrs_state": {}},
    )
    # MAX(reviewed_at) chronology probe -> None (no prior review recorded in the
    # mock), log INSERT RETURNING id -> 123. See test_sync_today_... above.
    conn.fetchval = AsyncMock(
        side_effect=lambda q, *a, **k: None if "MAX(reviewed_at)" in q else 123
    )
    handler = getattr(sync_reviews, "__wrapped__", sync_reviews)
    result = await handler(request=MagicMock(), body=body, db_pool=pool, user_id=1)

    assert result.synced == 2, f"both events should sync; got {result!r}"
    assert seen == sorted(seen), f"FSRS must see reviews oldest-first; got replay order {seen}"
    assert seen[0].astimezone(UTC) == older, (
        f"first replayed event must be the older one; got {seen[0]}"
    )
