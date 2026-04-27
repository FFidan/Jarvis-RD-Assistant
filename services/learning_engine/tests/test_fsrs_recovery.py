"""Tests for FSRSManager corrupt state recovery.

Verifies that schedule_review gracefully handles invalid fsrs_state
by falling back to a new Card instead of crashing.
"""

from __future__ import annotations

import logging

import pytest
from fsrs import Rating
from learning_engine.fsrs_manager import FSRSManager


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

    def test_fsrs_invalid_state_logs_warning_and_resets(
        self, manager: FSRSManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """LE-003: corrupt fsrs_state must emit a WARNING log before resetting to new card."""
        corrupt_state = {"garbage": True}
        card_id = 42

        with caplog.at_level(logging.WARNING, logger="learning_engine.fsrs_manager"):
            new_state, review_log, next_due = manager.schedule_review(
                corrupt_state, Rating.Good, card_id=card_id
            )

        # Result is still valid
        assert isinstance(new_state, dict)
        assert isinstance(review_log, dict)
        assert next_due is not None

        # A warning must have been emitted
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "Expected at least one WARNING log from fsrs_manager"

        warning_text = warning_records[0].getMessage()
        # Message should mention the corrupt state repr and the card id
        assert repr(corrupt_state) in warning_text or str(corrupt_state) in warning_text
        assert str(card_id) in warning_text

    def test_fsrs_attr_error_falls_back_gracefully(self, manager: FSRSManager) -> None:
        """M10: AttributeError from Card.from_dict must be caught and reset gracefully.

        Some corrupt FSRS state shapes trigger AttributeError inside the fsrs
        library (e.g. when an attribute is accessed on a wrong type). The
        except clause must now include AttributeError so these states reset
        to a new Card instead of propagating the exception.
        """
        from unittest.mock import patch

        with patch("learning_engine.fsrs_manager.Card") as mock_card:
            # Make Card.from_dict raise AttributeError (corrupt state triggers
            # attribute access on a non-Card object inside the fsrs library)
            mock_card.from_dict.side_effect = AttributeError(
                "'NoneType' object has no attribute 'state'"
            )
            # Card() (fallback) must return a real Card so the scheduler works
            from fsrs import Card as RealCard

            mock_card.return_value = RealCard()

            new_state, review_log, next_due = manager.schedule_review(
                {"state": None}, rating=3, card_id=99
            )

        assert isinstance(new_state, dict)
        assert isinstance(review_log, dict)
        assert next_due is not None
