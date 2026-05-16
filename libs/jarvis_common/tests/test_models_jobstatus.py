"""Tests for JobStatusResponse model type correctness."""

from __future__ import annotations

import pytest
from jarvis_common.models import JobStatusResponse
from pydantic import ValidationError


def test_job_status_response_user_id_accepts_int():
    """JobStatusResponse.user_id accepts int (from JSONB job args)."""
    # This should NOT raise ValidationError
    response = JobStatusResponse(
        id="job-123",
        kind="paper.summarize",
        status="done",
        user_id=1,
    )
    assert response.user_id == 1
    assert isinstance(response.user_id, int)


def test_job_status_response_user_id_accepts_none():
    """JobStatusResponse.user_id accepts None."""
    response = JobStatusResponse(
        id="job-123",
        kind="paper.summarize",
        status="done",
        user_id=None,
    )
    assert response.user_id is None


def test_job_status_response_user_id_rejects_string():
    """JobStatusResponse.user_id does not accept string (it's an int field)."""
    with pytest.raises(ValidationError):
        JobStatusResponse(
            id="job-123",
            kind="paper.summarize",
            status="done",
            user_id="not-an-int",
        )
