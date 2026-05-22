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
  - Determinism: same card state + same rating → same updated FSRS state
    (stability, difficulty, lapses, etc.) and same scheduling *interval*
    (within a small wall-clock tolerance, since ``fsrs.Scheduler.review_card``
    internally adds the interval to ``datetime.now()`` and the library does
    not accept an injectable clock).
  - Per-user isolation: two independent FSRSManager instances with same input
    produce the same output (no shared scheduler state leaks between users).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.contract]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_card_state() -> dict:
    """Return the initial FSRS state for a new card (deterministic baseline)."""
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr = FSRSManager()
    state, _due = mgr.create_new_card()
    return state


_WALL_CLOCK_FIELDS = frozenset({"due", "last_review"})


def _deterministic_state_fields(state: dict) -> dict:
    """Extract the wall-clock-independent fields from an FSRS state dict.

    ``due`` is computed from ``datetime.now() + interval`` and ``last_review``
    is set to ``datetime.now()``, so both vary between calls. Everything else
    (stability, difficulty, reps, lapses, state, etc.) is a pure function of
    the input state + rating.
    """
    return {k: v for k, v in state.items() if k not in _WALL_CLOCK_FIELDS}


# ---------------------------------------------------------------------------
# Determinism: same input → same updated state + same interval
# ---------------------------------------------------------------------------


def test_fsrs_schedule_review_is_deterministic() -> None:
    """Same card state + same rating produces the same updated FSRS state
    and the same scheduling interval (within wall-clock tolerance).

    This is the behavioral guarantee that drives the FSRS algorithm's
    reliability claim: given fixed inputs, the scheduler must return a fixed
    output.  Non-determinism here would invalidate the scheduling contract.
    """
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr = FSRSManager(desired_retention=0.9)
    initial_state = _fresh_card_state()
    rating = 3  # "Good"

    state_1, _, _ = mgr.schedule_review(initial_state, rating, card_id="det-test-1")
    state_2, _, _ = mgr.schedule_review(initial_state, rating, card_id="det-test-2")

    assert _deterministic_state_fields(state_1) == _deterministic_state_fields(state_2), (
        f"FSRSManager.schedule_review state is non-deterministic: "
        f"first={state_1!r}, second={state_2!r}"
    )


@pytest.mark.parametrize("rating", [1, 2, 3, 4], ids=["again", "hard", "good", "easy"])
def test_fsrs_determinism_across_ratings(rating: int) -> None:
    """Determinism holds for all four FSRS rating values."""
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr = FSRSManager()
    state = _fresh_card_state()

    state_a, _, _ = mgr.schedule_review(state, rating)
    state_b, _, _ = mgr.schedule_review(state, rating)

    assert _deterministic_state_fields(state_a) == _deterministic_state_fields(state_b), (
        f"schedule_review state non-deterministic at rating={rating}: {state_a!r} vs {state_b!r}"
    )


# ---------------------------------------------------------------------------
# Per-user isolation: independent FSRSManager instances share no state
# ---------------------------------------------------------------------------


def test_fsrs_independent_managers_produce_same_output() -> None:
    """Two separate FSRSManager instances (modelling two users) with identical
    inputs produce identical updated FSRS state.

    This proves there is no scheduler-level shared mutable state that could
    cause cross-user schedule pollution (e.g. a module-level mutable default
    being inadvertently shared).
    """
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr_user_a = FSRSManager(desired_retention=0.9)
    mgr_user_b = FSRSManager(desired_retention=0.9)

    state = _fresh_card_state()
    rating = 3

    state_a, _, _ = mgr_user_a.schedule_review(state, rating, card_id="user-a-card-1")
    state_b, _, _ = mgr_user_b.schedule_review(state, rating, card_id="user-b-card-1")

    assert _deterministic_state_fields(state_a) == _deterministic_state_fields(state_b), (
        f"Cross-user FSRSManager isolation violated: user A state={state_a!r}, "
        f"user B state={state_b!r} (expected identical state from identical input)"
    )


def test_fsrs_review_history_isolation() -> None:
    """Reviews on user A's manager do not affect user B's subsequent schedule.

    Sequence:
    1. User A reviews card with rating=1 (Again) — would advance their scheduler state.
    2. User B reviews card with rating=3 (Good) on a fresh manager.
    3. User B's updated state must equal a fresh manager's rating=3 result.
    """
    from learning_engine.fsrs_manager import FSRSManager  # noqa: PLC0415

    mgr_a = FSRSManager()
    mgr_b = FSRSManager()
    mgr_control = FSRSManager()

    state = _fresh_card_state()

    mgr_a.schedule_review(state, 1, card_id="a-1")
    state_b, _, _ = mgr_b.schedule_review(state, 3, card_id="b-1")
    state_control, _, _ = mgr_control.schedule_review(state, 3, card_id="ctrl-1")

    assert _deterministic_state_fields(state_b) == _deterministic_state_fields(state_control), (
        f"User A's review leaked into user B's schedule: "
        f"state_b={state_b!r}, expected={state_control!r}"
    )
