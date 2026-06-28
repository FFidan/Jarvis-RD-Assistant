"""Model-level validation bounds.

review_duration_ms is an untrusted client-supplied integer. It must be bounded
to a sane range (0 .. 86_400_000 ms == one day) so a hostile or buggy client
cannot store an absurd duration. These tests pin the upper bound on every model
that carries the field.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from learning_engine.models import _MAX_REVIEW_DURATION_MS, Rating, ReviewRequest, ReviewSyncEvent


def test_review_request_duration_at_upper_bound_accepted():
    req = ReviewRequest(rating=Rating.GOOD, review_duration_ms=_MAX_REVIEW_DURATION_MS)
    assert req.review_duration_ms == _MAX_REVIEW_DURATION_MS


def test_review_request_duration_above_upper_bound_rejected():
    with pytest.raises(ValidationError):
        ReviewRequest(rating=Rating.GOOD, review_duration_ms=_MAX_REVIEW_DURATION_MS + 1)


def test_review_sync_event_duration_at_upper_bound_accepted():
    event = ReviewSyncEvent(
        idempotency_key="k1",
        card_id=1,
        rating=Rating.GOOD,
        reviewed_at=datetime.now(UTC),
        review_duration_ms=_MAX_REVIEW_DURATION_MS,
    )
    assert event.review_duration_ms == _MAX_REVIEW_DURATION_MS


def test_review_sync_event_duration_above_upper_bound_rejected():
    with pytest.raises(ValidationError):
        ReviewSyncEvent(
            idempotency_key="k1",
            card_id=1,
            rating=Rating.GOOD,
            reviewed_at=datetime.now(UTC),
            review_duration_ms=_MAX_REVIEW_DURATION_MS + 1,
        )


def test_review_request_duration_below_lower_bound_rejected():
    """review_duration_ms=-1 must raise ValidationError (ge=0 lower bound)."""
    with pytest.raises(ValidationError):
        ReviewRequest(rating=Rating.GOOD, review_duration_ms=-1)


def test_review_sync_event_duration_below_lower_bound_rejected():
    """review_duration_ms=-1 must raise ValidationError on ReviewSyncEvent (ge=0 lower bound)."""
    with pytest.raises(ValidationError):
        ReviewSyncEvent(
            idempotency_key="k1",
            card_id=1,
            rating=Rating.GOOD,
            reviewed_at=datetime.now(UTC),
            review_duration_ms=-1,
        )


def test_review_sync_event_far_future_reviewed_at_clamped_to_now():
    """A far-future reviewed_at is clamped to ~now so it cannot push due_at forward."""
    from datetime import timedelta

    far_future = datetime.now(UTC) + timedelta(days=365)
    event = ReviewSyncEvent(
        idempotency_key="k1", card_id=1, rating=Rating.GOOD, reviewed_at=far_future
    )
    assert event.reviewed_at <= datetime.now(UTC) + timedelta(seconds=5)


def test_review_sync_event_past_reviewed_at_unchanged():
    """A past reviewed_at passes through untouched."""
    from datetime import timedelta

    past = datetime.now(UTC) - timedelta(hours=3)
    event = ReviewSyncEvent(idempotency_key="k1", card_id=1, rating=Rating.GOOD, reviewed_at=past)
    assert event.reviewed_at == past


def test_review_sync_event_minor_future_skew_within_tolerance_unchanged():
    """A reviewed_at a minute ahead (client clock skew) is within tolerance, not clamped."""
    from datetime import timedelta

    near = datetime.now(UTC) + timedelta(seconds=60)
    event = ReviewSyncEvent(idempotency_key="k1", card_id=1, rating=Rating.GOOD, reviewed_at=near)
    assert event.reviewed_at == near
