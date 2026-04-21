"""Tests for the jobs REST router (create, get, list, cancel)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from learning_engine.routers import jobs as jobs_router  # noqa: E402
from learning_engine.routers.jobs import CreateJobRequest  # noqa: E402
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# CreateJobRequest validator
# ---------------------------------------------------------------------------


def test_create_job_request_rejects_blank_kind():
    with pytest.raises(ValidationError, match="kind must be a non-empty string"):
        CreateJobRequest(kind="   ")


def test_create_job_request_accepts_valid_kind():
    req = CreateJobRequest(kind="card.generate", payload={"paper_id": 1})
    assert req.kind == "card.generate"
    assert req.payload == {"paper_id": 1}


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_rejects_disallowed_kind(monkeypatch):
    """POST /api/jobs with an unknown kind raises 422."""
    monkeypatch.delenv("DEV_MODE", raising=False)

    mock_request = MagicMock()
    mock_request.app.state.db_pool = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.create_job.__wrapped__(
            mock_request,
            body=CreateJobRequest(kind="secret.internal"),
            user_id=None,
        )

    assert exc_info.value.status_code == 422
    assert "secret.internal" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_job_enqueues_allowed_kind(monkeypatch):
    """POST /api/jobs with card.generate enqueues and returns job_id."""
    monkeypatch.delenv("DEV_MODE", raising=False)

    mock_request = MagicMock()
    mock_request.app.state.db_pool = MagicMock()

    with patch.object(jobs_router.jobs_lib, "enqueue", AsyncMock(return_value="abc-123")):
        result = await jobs_router.create_job.__wrapped__(
            mock_request,
            body=CreateJobRequest(kind="card.generate"),
            user_id=42,
        )

    assert result == {"job_id": "abc-123", "status": "queued"}


# ---------------------------------------------------------------------------
# get_job — 404 on missing + job-ownership enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_returns_404_when_not_found():
    mock_request = MagicMock()
    mock_request.app.state.db_pool = MagicMock()

    with patch.object(jobs_router.jobs_lib, "get", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await jobs_router.get_job.__wrapped__(
                mock_request,
                job_id="missing-id",
                user_id=1,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_job_returns_404_for_wrong_owner():
    """A job owned by user 7 is invisible to user 99."""
    mock_request = MagicMock()
    mock_request.app.state.db_pool = MagicMock()
    row = {"id": "job-1", "user_id": 7, "status": "done", "kind": "card.generate", "payload": {}}

    with patch.object(jobs_router.jobs_lib, "get", AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc_info:
            await jobs_router.get_job.__wrapped__(
                mock_request,
                job_id="job-1",
                user_id=99,
            )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_jobs — status filter pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_passes_status_filter_to_lib():
    mock_request = MagicMock()
    mock_request.app.state.db_pool = MagicMock()
    rows = [
        {"id": "j1", "status": "queued", "kind": "card.generate", "payload": {}, "user_id": None}
    ]

    with patch.object(jobs_router.jobs_lib, "list_jobs", AsyncMock(return_value=rows)) as mock_list:
        result = await jobs_router.list_jobs.__wrapped__(
            mock_request,
            status="queued",
            kind=None,
            limit=10,
            user_id=5,
        )

    mock_list.assert_awaited_once()
    assert mock_list.await_args is not None
    call_kwargs = mock_list.await_args.kwargs
    assert call_kwargs["status"] == "queued"
    assert len(result) == 1
