"""POST /api/review/sync — idempotent offline review replay (contract 2026-05-16).

Covers contract §8: new-key applies (FSRS recompute + cards/review_logs writes
incl. idempotency_key + client reviewed_at), duplicate-key → synced no
re-apply, card-not-owned → skipped (not 404), empty batch → {0,0}, ordered
multi-event same card threads FSRS forward, cross-user isolation, auth
dependency present. Regression-guards submit_review unchanged.

Mock pattern mirrors test_cards_scoping.py + conftest mock_db/FakeRecord:
handler is invoked via ``.__wrapped__`` (bypass the rate-limit decorator) with
``user_id`` passed explicitly and a SimpleNamespace request.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from learning_engine.models import ReviewSyncEvent, ReviewSyncRequest
from learning_engine.routers import review

from tests.conftest import FakeRecord


def _now() -> datetime:
    return datetime.now(UTC)


def _make_pool_conn():
    """Mock asyncpg pool/conn with an async-ctx transaction (per conftest)."""
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


def _card_row(card_id: int = 1, fsrs_state: dict | None = None) -> FakeRecord:
    return FakeRecord(
        id=card_id,
        fsrs_state=fsrs_state if fsrs_state is not None else {"reps": 0},
        due_at=_now(),
        updated_at=_now(),
    )


def _event(
    key: str, *, card_id: int = 1, rating: int = 3, dur: int | None = 8421
) -> ReviewSyncEvent:
    return ReviewSyncEvent(
        idempotency_key=key,
        card_id=card_id,
        rating=rating,
        reviewed_at=_now() - timedelta(hours=2),
        review_duration_ms=dur,
    )


def _patch_fsrs(monkeypatch, *, recorder: list | None = None) -> None:
    """Stub _build_fsrs_manager_from_db → manager whose schedule_review is deterministic."""

    async def _fake_build(conn, user_id=None):  # noqa: ARG001
        mgr = MagicMock()

        def _schedule(state, rating):
            reps = (state or {}).get("reps", 0) + 1
            new_state = {"reps": reps, "last_rating": rating}
            if recorder is not None:
                recorder.append((dict(state), rating, new_state))
            return new_state, {"log": True, "rating": rating}, _now() + timedelta(days=reps)

        mgr.schedule_review.side_effect = _schedule
        return mgr

    monkeypatch.setattr(review, "_build_fsrs_manager_from_db", _fake_build)


# ---------------------------------------------------------------------------
# Contract §8 behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_batch_returns_zero_zero() -> None:
    """Empty `reviews` → {synced:0, skipped:0}; no DB acquired."""
    pool, conn = _make_pool_conn()
    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=1)),
        body=ReviewSyncRequest(reviews=[]),
        db_pool=pool,
        user_id=1,
    )
    assert resp.synced == 0
    assert resp.skipped == 0
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_new_key_applies_fsrs_and_both_writes(monkeypatch) -> None:
    """New key → FSRS recompute + UPDATE cards + INSERT review_logs with the
    client `reviewed_at` and `idempotency_key`; counted under synced."""
    pool, conn = _make_pool_conn()
    conn.fetch = AsyncMock(return_value=[])  # no keys applied yet
    # fetchrow serves the ownership SELECT; fetchval serves the dedupe-gated
    # INSERT ... RETURNING id (non-None → INSERT won, apply the card UPDATE).
    conn.fetchrow = AsyncMock(return_value=_card_row())
    conn.fetchval = AsyncMock(return_value=4242)
    conn.execute = AsyncMock(return_value=None)
    _patch_fsrs(monkeypatch)

    ev = _event("key-1")
    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=9)),
        body=ReviewSyncRequest(reviews=[ev]),
        db_pool=pool,
        user_id=9,
    )

    assert resp.synced == 1
    assert resp.skipped == 0

    # INSERT review_logs runs first (via fetchval RETURNING id), then the
    # dedupe-gated UPDATE cards (via execute) only because the INSERT won.
    insert_call = conn.fetchval.await_args
    insert_sql = insert_call.args[0]
    assert "INSERT INTO review_logs" in insert_sql
    assert "idempotency_key" in insert_sql
    assert "ON CONFLICT (user_id, idempotency_key)" in insert_sql
    assert "WHERE idempotency_key IS NOT NULL DO NOTHING" in insert_sql
    assert "RETURNING id" in insert_sql
    # INSERT positional args: card_id, rating, dur, reviewed_at, log, user_id, key
    assert insert_call.args[1] == ev.card_id
    assert insert_call.args[2] == 3
    assert insert_call.args[3] == ev.review_duration_ms
    assert insert_call.args[4] == ev.reviewed_at  # client wall-clock, authoritative
    assert insert_call.args[6] == 9  # scoped user
    assert insert_call.args[7] == "key-1"

    # Exactly one card UPDATE, applied because the INSERT won the conflict.
    assert conn.execute.await_count == 1
    update_call = conn.execute.await_args
    # UPDATE cards SET fsrs_state=$1, due_at=$2, ... WHERE id=$3
    assert "UPDATE cards SET fsrs_state" in update_call.args[0]
    assert update_call.args[3] == ev.card_id

    # Dedupe pre-check scoped to the caller.
    pre = conn.fetch.await_args
    assert pre.args[1] == 9
    assert pre.args[2] == ["key-1"]


@pytest.mark.asyncio
async def test_duplicate_key_counts_synced_without_reapply(monkeypatch) -> None:
    """A previously-applied key → synced, no FSRS recompute, no writes."""
    pool, conn = _make_pool_conn()
    conn.fetch = AsyncMock(return_value=[FakeRecord(idempotency_key="dup")])
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    build_spy = AsyncMock()
    monkeypatch.setattr(review, "_build_fsrs_manager_from_db", build_spy)

    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=2)),
        body=ReviewSyncRequest(reviews=[_event("dup")]),
        db_pool=pool,
        user_id=2,
    )

    assert resp.synced == 1
    assert resp.skipped == 0
    conn.fetchrow.assert_not_called()
    conn.execute.assert_not_called()
    build_spy.assert_not_called()


@pytest.mark.asyncio
async def test_card_not_owned_is_skipped_not_404(monkeypatch) -> None:
    """Card missing / not owned → skipped, batch still returns 200 (no raise)."""
    pool, conn = _make_pool_conn()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)  # no row for this user
    conn.execute = AsyncMock()
    _patch_fsrs(monkeypatch)

    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=3)),
        body=ReviewSyncRequest(reviews=[_event("k", card_id=999)]),
        db_pool=pool,
        user_id=3,
    )

    assert resp.synced == 0
    assert resp.skipped == 1
    conn.execute.assert_not_called()  # no writes for a skipped event
    # Ownership SELECT scoped to caller.
    assert conn.fetchrow.await_args.args[1] == 999
    assert conn.fetchrow.await_args.args[2] == 3


@pytest.mark.asyncio
async def test_ordered_multi_event_same_card_threads_fsrs_forward(monkeypatch) -> None:
    """Two ordered events on one card: event 2's input state = event 1's output."""
    pool, conn = _make_pool_conn()
    conn.fetch = AsyncMock(return_value=[])
    # Re-SELECT per event returns the latest persisted fsrs_state.
    states = [{"reps": 0}, {"reps": 1, "last_rating": 3}]
    conn.fetchrow = AsyncMock(side_effect=[_card_row(fsrs_state=s) for s in states])
    conn.execute = AsyncMock()
    recorder: list = []
    _patch_fsrs(monkeypatch, recorder=recorder)

    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=4)),
        body=ReviewSyncRequest(reviews=[_event("a", rating=3), _event("b", rating=4)]),
        db_pool=pool,
        user_id=4,
    )

    assert resp.synced == 2
    assert resp.skipped == 0
    assert len(recorder) == 2
    # Event 2 fed the state event 1 produced (reps threaded forward).
    in1, r1, out1 = recorder[0]
    in2, r2, out2 = recorder[1]
    assert in1 == {"reps": 0}
    assert in2 == out1  # threaded forward
    assert (r1, r2) == (3, 4)


@pytest.mark.asyncio
async def test_cross_user_isolation(monkeypatch) -> None:
    """User B syncing only ever scopes to user B (dedupe + ownership + writes)."""
    pool, conn = _make_pool_conn()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=_card_row())
    conn.fetchval = AsyncMock(return_value=1)  # INSERT won
    conn.execute = AsyncMock()
    _patch_fsrs(monkeypatch)

    await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=77)),
        body=ReviewSyncRequest(reviews=[_event("u77")]),
        db_pool=pool,
        user_id=77,
    )

    assert conn.fetch.await_args.args[1] == 77  # dedupe pre-check user
    assert conn.fetchrow.await_args.args[2] == 77  # ownership SELECT user
    insert_call = conn.fetchval.await_args  # review_logs INSERT ... RETURNING id
    assert insert_call.args[6] == 77  # review_logs.user_id


@pytest.mark.asyncio
async def test_partial_resend_converges(monkeypatch) -> None:
    """Already-applied key → synced no-op; not-yet-applied key → applied now."""
    pool, conn = _make_pool_conn()
    conn.fetch = AsyncMock(return_value=[FakeRecord(idempotency_key="old")])
    conn.fetchrow = AsyncMock(return_value=_card_row())
    conn.fetchval = AsyncMock(return_value=99)  # INSERT won for "new"
    conn.execute = AsyncMock()
    _patch_fsrs(monkeypatch)

    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=5)),
        body=ReviewSyncRequest(reviews=[_event("old"), _event("new")]),
        db_pool=pool,
        user_id=5,
    )

    assert resp.synced == 2  # old (no-op) + new (applied)
    assert resp.skipped == 0
    # Only the new key triggered the INSERT + dedupe-gated card UPDATE.
    assert conn.fetchval.await_count == 1
    assert conn.fetchval.await_args.args[7] == "new"
    assert conn.execute.await_count == 1  # one card UPDATE for "new"
    assert "UPDATE cards SET fsrs_state" in conn.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_concurrent_duplicate_insert_conflict_does_not_double_advance(
    monkeypatch,
) -> None:
    """A key that slips past the pre-batch dedupe SELECT but whose INSERT then
    hits ON CONFLICT (concurrent duplicate) must NOT issue the cards UPDATE
    (no FSRS double-advance); it is still counted under synced (contract §4)."""
    pool, conn = _make_pool_conn()
    conn.fetch = AsyncMock(return_value=[])  # pre-SELECT saw nothing applied
    conn.fetchrow = AsyncMock(return_value=_card_row())
    # INSERT ... ON CONFLICT DO NOTHING RETURNING id → no row (dupe won the race).
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    _patch_fsrs(monkeypatch)

    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=8)),
        body=ReviewSyncRequest(reviews=[_event("race-key")]),
        db_pool=pool,
        user_id=8,
    )

    # Recorded by the concurrent winner → counted synced, not skipped.
    assert resp.synced == 1
    assert resp.skipped == 0
    # The INSERT was attempted (and conflicted) ...
    assert conn.fetchval.await_count == 1
    assert conn.fetchval.await_args.args[7] == "race-key"
    # ... but the cards UPDATE was NOT applied — no FSRS double-advance.
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Auth dependency present (contract §6/§8) + submit_review regression guard
# ---------------------------------------------------------------------------


def _dep_default(func, name: str):
    param = inspect.signature(func).parameters[name]
    return param.default


def test_sync_reviews_uses_strict_owner_override_auth() -> None:
    """user_id is injected via current_user_id_strict_with_owner_override."""
    from jarvis_common.auth import current_user_id_strict_with_owner_override

    dep = _dep_default(review.sync_reviews.__wrapped__, "user_id")
    assert dep.dependency is current_user_id_strict_with_owner_override


def test_sync_reviews_route_registered() -> None:
    """POST /api/review/sync mounted on the review router."""
    paths = {(route.path, tuple(sorted(route.methods))) for route in review.router.routes}
    assert ("/api/review/sync", ("POST",)) in paths


@pytest.mark.asyncio
async def test_build_fsrs_manager_swallows_narrowed_parse_errors() -> None:
    """LE-OB5: unparseable config values (ValueError) are logged & defaulted,
    not raised — and the except is narrowed (no bare Exception)."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            FakeRecord(key="fsrs.desired_retention", value="not-a-float"),
            FakeRecord(key="fsrs.learning_steps", value="not-json"),
        ]
    )
    # No raise: both unparseable values are swallowed (logged) and defaults used.
    mgr = await review._build_fsrs_manager_from_db(conn, user_id=1)
    assert mgr.scheduler.desired_retention == 0.9  # fell back to default

    src = inspect.getsource(review._build_fsrs_manager_from_db)
    assert "except (ValueError, json.JSONDecodeError, TypeError):" in src
    assert "except Exception:" not in src


def test_submit_review_unchanged_regression() -> None:
    """submit_review still uses the same auth dep and per-card path (untouched)."""
    from jarvis_common.auth import current_user_id_strict_with_owner_override

    dep = _dep_default(review.submit_review.__wrapped__, "user_id")
    assert dep.dependency is current_user_id_strict_with_owner_override
    src = inspect.getsource(review.submit_review)
    assert 'raise HTTPException(status_code=404, detail="Card not found")' in src
    assert "RETURNING id" in src  # still single-shot log_id fetchval
