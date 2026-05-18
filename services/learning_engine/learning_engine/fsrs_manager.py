"""FSRS spaced repetition scheduler wrapper.

Thin wrapper around py-fsrs that handles serialization between
our JSONB database storage and the fsrs library's Card objects.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, cast

from fsrs import Card, Rating, Scheduler

logger = logging.getLogger(__name__)


class FSRSManager:
    """Manage FSRS card scheduling and review operations."""

    def __init__(
        self,
        desired_retention: float = 0.9,
        learning_steps: list[timedelta] | None = None,
    ) -> None:
        """Initialise the FSRS scheduler.

        Parameters
        ----------
        desired_retention : float
            Target long-term retention rate (0–1).  Defaults to 0.9 (90 %).
            Review endpoints build a fresh manager from live ``user_config``
            values so that per-user edits take effect immediately.
        learning_steps : list[timedelta] | None
            Intraday learning steps.  ``None`` falls back to
            ``[1 min, 10 min]`` — the py-fsrs default-compatible pair.
        """
        _steps = (
            learning_steps
            if learning_steps is not None
            else [timedelta(minutes=1), timedelta(minutes=10)]
        )
        self.scheduler = Scheduler(desired_retention=desired_retention, learning_steps=_steps)

    def create_new_card(self) -> tuple[dict, datetime]:
        """Create a new FSRS card and return its initial state.

        Returns
        -------
        tuple[dict, datetime]
            (fsrs_state_dict, due_at) for database insertion.
        """
        card = Card()
        return dict(card.to_dict()), card.due

    def schedule_review(
        self, fsrs_state: dict, rating: int, card_id: int | str | None = None
    ) -> tuple[dict, dict, datetime]:
        """Schedule a review and return updated state.

        Parameters
        ----------
        fsrs_state : dict
            Current card state from database (JSONB).
        rating : int
            User rating: 1=Again, 2=Hard, 3=Good, 4=Easy.

        Returns
        -------
        tuple[dict, dict, datetime]
            (new_fsrs_state, review_log_dict, next_due_at).
        """
        try:
            card = Card.from_dict(cast(Any, fsrs_state))
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning(
                "Invalid FSRS state %s for card %s, resetting", repr(fsrs_state), card_id
            )
            card = Card()
        fsrs_rating = Rating(rating)
        new_card, review_log = self.scheduler.review_card(card, fsrs_rating)
        return dict(new_card.to_dict()), dict(review_log.to_dict()), new_card.due
