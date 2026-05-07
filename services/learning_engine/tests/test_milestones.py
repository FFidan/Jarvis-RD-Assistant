"""Smoke tests for MilestoneCreate / MilestoneResponse models (W1-10).

Verifies that:
- MilestoneCreate requires a deadline (POST without deadline → 422, not 500)
- MilestoneCreate accepts a valid datetime deadline
- MilestoneResponse accepts a nullable datetime deadline
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from learning_engine.models import MilestoneCreate, MilestoneResponse  # noqa: E402
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# MilestoneCreate
# ---------------------------------------------------------------------------


def test_milestone_create_requires_deadline():
    """MilestoneCreate.deadline is required — omitting it raises ValidationError (→ 422)."""
    with pytest.raises(ValidationError) as exc_info:
        MilestoneCreate(name="Ship v2")
    errors = exc_info.value.errors()
    field_names = [e["loc"][-1] for e in errors]
    assert "deadline" in field_names, f"Expected 'deadline' missing error, got: {errors}"


def test_milestone_create_accepts_datetime_deadline():
    """MilestoneCreate accepts a timezone-aware datetime for deadline."""
    deadline = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)
    m = MilestoneCreate(name="Ship v2", deadline=deadline)
    assert m.deadline == deadline
    assert m.name == "Ship v2"


def test_milestone_create_optional_description():
    """MilestoneCreate.description is optional."""
    deadline = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)
    m = MilestoneCreate(name="Ship v2", deadline=deadline, description="Public release")
    assert m.description == "Public release"

    m_no_desc = MilestoneCreate(name="Ship v2", deadline=deadline)
    assert m_no_desc.description is None


# ---------------------------------------------------------------------------
# MilestoneResponse
# ---------------------------------------------------------------------------


def test_milestone_response_deadline_nullable():
    """MilestoneResponse.deadline may be None (existing rows without deadline)."""
    now = datetime.now(UTC)
    resp = MilestoneResponse(
        id=1,
        project_id=10,
        name="Milestone A",
        deadline=None,
        created_at=now,
    )
    assert resp.deadline is None


def test_milestone_response_deadline_datetime():
    """MilestoneResponse.deadline accepts a datetime value."""
    now = datetime.now(UTC)
    deadline = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    resp = MilestoneResponse(
        id=2,
        project_id=10,
        name="Milestone B",
        deadline=deadline,
        created_at=now,
    )
    assert resp.deadline == deadline
