"""Tests for FSRSManager corrupt state recovery.

Verifies that schedule_review gracefully handles invalid fsrs_state
by falling back to a new Card instead of crashing.
"""

from __future__ import annotations

import pytest
from app.fsrs_manager import FSRSManager
from fsrs import Rating


@pytest.fixture
def manager() -> FSRSManager:
    return FSRSManager(desired_retention=0.9)


class TestScheduleReviewValidState:
    """schedule_review processes valid fsrs_state normally."""

    def test_valid_card_round_trips(self, manager: FSRSManager) -> None:
        """A freshly-created card state should be accepted and updated."""
        state, _due = manager.create_new_card()

        new_state, review_log, next_due = manager.schedule_review(state, Rating.Good)

        assert isinstance(new_state, dict)
        assert isinstance(review_log, dict)
        assert next_due is not None
        # The state should have changed after a review
        assert new_state != state

    def test_multiple_reviews(self, manager: FSRSManager) -> None:
        """Multiple sequential reviews should work without error."""
        state, _ = manager.create_new_card()

        for rating in [Rating.Good, Rating.Hard, Rating.Easy, Rating.Again]:
            state, _log, _due = manager.schedule_review(state, rating)

        assert isinstance(state, dict)


class TestScheduleReviewCorruptState:
    """schedule_review falls back to a new Card for corrupt/invalid state."""

    @pytest.mark.parametrize(
        "corrupt_state,description",
        [
            ({"garbage": True}, "dict with wrong keys"),
            ({}, "empty dict"),
            ({"stability": "not_a_number", "difficulty": "bad"}, "non-numeric values"),
            (
                {
                    "due": "invalid-date",
                    "stability": 0,
                    "difficulty": 0,
                    "elapsed_days": 0,
                    "scheduled_days": 0,
                    "reps": 0,
                    "lapses": 0,
                    "state": 99,
                },
                "invalid state enum",
            ),
        ],
        ids=["garbage_keys", "empty_dict", "non_numeric_values", "invalid_state_enum"],
    )
    def test_corrupt_dict_falls_back_to_new_card(
        self, manager: FSRSManager, corrupt_state: dict, description: str
    ) -> None:
        """Corrupt dict fsrs_state should not crash; returns valid results."""
        new_state, review_log, next_due = manager.schedule_review(corrupt_state, Rating.Good)

        assert isinstance(new_state, dict), f"Failed for {description}"
        assert isinstance(review_log, dict), f"Failed for {description}"
        assert next_due is not None, f"Failed for {description}"

    def test_string_state_falls_back(self, manager: FSRSManager) -> None:
        """A string instead of a dict should trigger fallback."""
        new_state, review_log, next_due = manager.schedule_review(
            "not a dict",
            Rating.Good,  # type: ignore[arg-type]
        )

        assert isinstance(new_state, dict)
        assert isinstance(review_log, dict)
        assert next_due is not None

    def test_none_state_falls_back(self, manager: FSRSManager) -> None:
        """None fsrs_state should trigger fallback."""
        new_state, review_log, next_due = manager.schedule_review(
            None,
            Rating.Good,  # type: ignore[arg-type]
        )

        assert isinstance(new_state, dict)
        assert isinstance(review_log, dict)
        assert next_due is not None

    def test_corrupt_state_produces_same_result_as_new_card(self, manager: FSRSManager) -> None:
        """Corrupt state fallback should behave identically to a fresh card review."""
        # Review a fresh card
        fresh_state, _ = manager.create_new_card()
        fresh_result, fresh_log, fresh_due = manager.schedule_review(fresh_state, Rating.Good)

        # Review with corrupt state (also falls back to Card())
        corrupt_result, corrupt_log, corrupt_due = manager.schedule_review(
            {"garbage": True}, Rating.Good
        )

        # Both should produce structurally identical results
        assert set(fresh_result.keys()) == set(corrupt_result.keys())
        assert set(fresh_log.keys()) == set(corrupt_log.keys())
