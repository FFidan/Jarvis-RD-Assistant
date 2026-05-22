"""FSRS scheduling contract suite.

Exercises the FSRSManager pure-logic contracts that the mock tests can never
prove: determinism (same input → same output) and per-user independence.

These tests do NOT require the contract DB (no DB interaction); they use the
real FSRSManager against deterministic input states.  They are filed in the
contract suite because:
  a) they prove a durable behavioral contract (not an implementation detail),
  b) they belong with the other service-level predicate suites here.

The ``pytest.mark.contract`` marker still applies so they run together with
the rest of the contract suite.  Since they need no live Postgres, they are
NOT skipped when ``JARVIS_RUN_LIVE_PG`` is absent — the marker only gates
the ``contract_pg_dsn`` fixture, which these tests never request.

Predicate coverage:
  - Determinism: same card state + same rating → same next_due_date
  - Per-user isolation: two independent FSRSManager instances with same input
    produce the same output (no shared scheduler state leaks between users)
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_card_state() -> dict:
    """Return the initial FSRS state for a new card (deterministic baseline)."""
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr = FSRSManager()
    state, _due = mgr.create_new_card()
    return state


# ---------------------------------------------------------------------------
# Determinism: same input → same next_due_date
# ---------------------------------------------------------------------------


def test_fsrs_schedule_review_is_deterministic() -> None:
    """Same card state + same rating produces the same next_due_at.

    This is the behavioral guarantee that drives the FSRS algorithm's
    reliability claim: given fixed inputs, the scheduler must return a fixed
    output.  Non-determinism here would invalidate the scheduling contract.
    """
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr = FSRSManager(desired_retention=0.9)
    initial_state = _fresh_card_state()
    rating = 3  # "Good"

    _, _, due_1 = mgr.schedule_review(initial_state, rating, card_id="det-test-1")
    # Re-use the same initial_state (schedule_review does not mutate the input dict)
    _, _, due_2 = mgr.schedule_review(initial_state, rating, card_id="det-test-2")

    assert due_1 == due_2, (
        f"FSRSManager.schedule_review is non-deterministic: "
        f"first call returned {due_1!r}, second returned {due_2!r} "
        f"(same initial_state, same rating=3)"
    )


@pytest.mark.parametrize("rating", [1, 2, 3, 4], ids=["again", "hard", "good", "easy"])
def test_fsrs_determinism_across_ratings(rating: int) -> None:
    """Determinism holds for all four FSRS rating values."""
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr = FSRSManager()
    state = _fresh_card_state()

    _, _, due_a = mgr.schedule_review(state, rating)
    _, _, due_b = mgr.schedule_review(state, rating)

    assert due_a == due_b, (
        f"schedule_review non-deterministic at rating={rating}: {due_a!r} vs {due_b!r}"
    )


# ---------------------------------------------------------------------------
# Per-user isolation: independent FSRSManager instances share no state
# ---------------------------------------------------------------------------


def test_fsrs_independent_managers_produce_same_output() -> None:
    """Two separate FSRSManager instances (modelling two users) with identical
    inputs produce identical next_due_at.

    This proves there is no scheduler-level shared mutable state that could
    cause cross-user schedule pollution (e.g. a module-level mutable default
    being inadvertently shared).
    """
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr_user_a = FSRSManager(desired_retention=0.9)
    mgr_user_b = FSRSManager(desired_retention=0.9)

    state = _fresh_card_state()
    rating = 3

    _, _, due_a = mgr_user_a.schedule_review(state, rating, card_id="user-a-card-1")
    _, _, due_b = mgr_user_b.schedule_review(state, rating, card_id="user-b-card-1")

    assert due_a == due_b, (
        f"Cross-user FSRSManager isolation violated: user A due={due_a!r}, "
        f"user B due={due_b!r} (expected identical output from identical input)"
    )


def test_fsrs_review_history_isolation() -> None:
    """Reviews on user A's manager do not affect user B's subsequent schedule.

    Sequence:
    1. User A reviews card with rating=1 (Again) — advances their scheduler state.
    2. User B reviews card with rating=3 (Good) on a fresh manager.
    3. User B's next_due must equal a fresh manager's rating=3 result.
    """
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr_a = FSRSManager()
    mgr_b = FSRSManager()
    mgr_control = FSRSManager()

    state = _fresh_card_state()

    # User A reviews (Again) — mutates mgr_a's internal scheduler state if any
    mgr_a.schedule_review(state, 1, card_id="a-1")
    # User B reviews (Good) — should be unaffected by A's review
    _, _, due_b = mgr_b.schedule_review(state, 3, card_id="b-1")
    # Control: fresh manager, same Good review
    _, _, due_control = mgr_control.schedule_review(state, 3, card_id="ctrl-1")

    assert due_b == due_control, (
        f"User A's review leaked into user B's schedule: due_b={due_b!r}, expected={due_control!r}"
    )
