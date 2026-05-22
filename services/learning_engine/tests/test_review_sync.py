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

from tests.conftest import FakeRecord, _make_pool_and_conn


def _now() -> datetime:
    return datetime.now(UTC)


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
    pool, conn = _make_pool_and_conn()
    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=1)),
        body=ReviewSyncRequest(reviews=[]),
        db_pool=pool,
        user_id=1,
    )
    assert resp.synced == 0
    assert resp.skipped == 0
    pool.acquire.assert_not_called()


# test_new_key_applies_fsrs_and_both_writes deleted — SQL-text B1-09
# ("INSERT INTO review_logs" in sql, "ON CONFLICT (user_id, idempotency_key)" in sql,
#  "RETURNING id" in sql, positional-arg binding assertions);
# survivor: test_review_contract.py (A219) verifies idempotency_key prevents
# double-apply against real PostgreSQL.


@pytest.mark.asyncio
async def test_duplicate_key_counts_synced_without_reapply(monkeypatch) -> None:
    """A previously-applied key → synced, no FSRS recompute, no writes.

    Note: after the N+1 fix, _build_fsrs_manager_from_db is hoisted before the
    loop so it is called once per batch (even all-duplicate batches).  The
    contract guarantee is that schedule_review is NOT called for duplicate keys
    and no DB writes are issued — not that the manager is never built.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch = AsyncMock(return_value=[FakeRecord(idempotency_key="dup")])
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    build_spy = AsyncMock()
    # build_spy returns an AsyncMock manager; schedule_review will not be called.
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
    # Manager is built once (hoisted), but schedule_review is never called for dups.
    build_spy.assert_called_once()
    mgr = build_spy.return_value
    mgr.schedule_review.assert_not_called()


@pytest.mark.asyncio
async def test_card_not_owned_is_skipped_not_404(monkeypatch) -> None:
    """Card missing / not owned → skipped, batch still returns 200 (no raise)."""
    pool, conn = _make_pool_and_conn()
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
    pool, conn = _make_pool_and_conn()
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


# test_cross_user_isolation deleted — B1-09 positional-arg binding assertions
# (args[1]==77, args[2]==77, args[6]==77); survivor:
# test_review_contract.py::test_sync_reviews_user_b_event_skipped_for_user_a_card (A219).


@pytest.mark.asyncio
async def test_partial_resend_converges(monkeypatch) -> None:
    """Already-applied key → synced no-op; not-yet-applied key → applied now."""
    pool, conn = _make_pool_and_conn()
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


# test_concurrent_duplicate_insert_conflict_does_not_double_advance deleted —
# positional-arg binding assertion (conn.fetchval.await_args.args[7] == "race-key");
# survivor: test_review_contract.py (A219) covers concurrent-conflict via real
# PostgreSQL ON CONFLICT round-trip.


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


@pytest.mark.asyncio
async def test_sync_reviews_builds_fsrs_manager_exactly_once(monkeypatch) -> None:
    """_build_fsrs_manager_from_db must be called once per sync, not per event (N+1 guard).

    Pre-fix this fails with call_count == 2. Post-fix it passes with call_count == 1.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch = AsyncMock(return_value=[])  # no applied keys
    conn.fetchrow = AsyncMock(return_value=_card_row())
    conn.fetchval = AsyncMock(return_value=42)  # INSERT won
    conn.execute = AsyncMock(return_value=None)

    call_count = 0
    original_build = review._build_fsrs_manager_from_db

    async def counting_build(c, user_id=None):
        nonlocal call_count
        call_count += 1
        return await original_build(c, user_id=user_id)

    # Use a real FSRSManager stub via _patch_fsrs but wrap to count first
    async def counting_stub(c, user_id=None):  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        mgr = MagicMock()

        def _schedule(state, rating):
            reps = (state or {}).get("reps", 0) + 1
            new_state = {"reps": reps}
            return new_state, {"log": True}, _now() + timedelta(days=reps)

        mgr.schedule_review.side_effect = _schedule
        return mgr

    monkeypatch.setattr(review, "_build_fsrs_manager_from_db", counting_stub)

    resp = await review.sync_reviews.__wrapped__(
        SimpleNamespace(state=SimpleNamespace(user_id=99)),
        body=ReviewSyncRequest(
            reviews=[
                _event("k1", card_id=1, rating=3),
                _event("k2", card_id=1, rating=4),
            ]
        ),
        db_pool=pool,
        user_id=99,
    )

    assert resp.synced == 2, f"Expected 2 synced, got {resp.synced}"
    assert call_count == 1, (
        f"_build_fsrs_manager_from_db called {call_count} times for 2 events "
        f"(expected 1 — N+1 bug not fixed)"
    )
