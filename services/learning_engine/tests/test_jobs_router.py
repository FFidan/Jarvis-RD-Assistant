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
@pytest.mark.parametrize("bad_kind", ["secret.internal", "totally.unknown.kind"])
async def test_create_job_rejects_disallowed_kind(bad_kind, monkeypatch):
    """POST /api/jobs with an unknown kind raises 400.

    Parametrized over two distinct unknown-kind strings to confirm the guard is
    generic (not a hard-coded string match).
    B2-18: test_create_job_unsupported_kind_returns_400 collapsed into this parametrize;
    both original kind values are preserved as cases.
    """
    monkeypatch.delenv("DEV_MODE", raising=False)

    mock_request = MagicMock()
    mock_pool = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await jobs_router.create_job.__wrapped__(
            mock_request,
            body=CreateJobRequest(kind=bad_kind),
            user_id=None,
            db_pool=mock_pool,
        )

    assert exc_info.value.status_code == 400
    assert bad_kind in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_job_enqueues_allowed_kind(monkeypatch):
    """POST /api/jobs with card.generate dispatches via KIND_TO_TASK and returns job_id.

    After the B.4 Step 3 cutover, ``create_job`` dispatches via
    ``KIND_TO_TASK.defer_async`` for all 19 registered kinds (including
    ``card.generate``).  The test patches ``defer_async`` on the task object
    to avoid a live procrastinate connection.
    """
    monkeypatch.delenv("DEV_MODE", raising=False)

    mock_request = MagicMock()
    mock_pool = MagicMock()

    fake_task = AsyncMock()
    fake_task.defer_async = AsyncMock(return_value=None)
    fake_kind_to_task = {"card.generate": fake_task}

    with patch.dict("jarvis_common.task_registry._TASK_MAP", fake_kind_to_task, clear=True):
        result = await jobs_router.create_job.__wrapped__(
            mock_request,
            body=CreateJobRequest(kind="card.generate"),
            user_id=42,
            db_pool=mock_pool,
        )

    assert result.status == "queued"
    assert isinstance(result.job_id, str)
    call_kwargs = fake_task.defer_async.await_args.kwargs
    assert call_kwargs["user_id"] == 42
    assert "job_id" in call_kwargs


# ---------------------------------------------------------------------------
# get_job — 404 on missing + job-ownership enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_returns_404_when_not_found():
    """GET /api/jobs/{id} → 404 when neither legacy nor procrastinate table has the job."""
    mock_request = MagicMock()
    mock_pool = MagicMock()

    # Patch get_unified so the route sees no row.
    with patch.object(jobs_router.jobs_lib, "get_unified", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await jobs_router.get_job.__wrapped__(
                mock_request,
                job_id="missing-id",
                user_id=1,
                db_pool=mock_pool,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_job_returns_404_for_wrong_owner():
    """A job owned by user 7 is invisible to user 99."""
    mock_request = MagicMock()
    mock_pool = MagicMock()
    row = {"id": "job-1", "user_id": 7, "status": "done", "kind": "card.generate", "payload": {}}

    with patch.object(jobs_router.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc_info:
            await jobs_router.get_job.__wrapped__(
                mock_request,
                job_id="job-1",
                user_id=99,
                db_pool=mock_pool,
            )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_jobs — status filter pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_passes_status_filter_to_lib():
    mock_request = MagicMock()
    mock_pool = MagicMock()
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
            db_pool=mock_pool,
        )

    mock_list.assert_awaited_once()
    assert mock_list.await_args is not None
    call_kwargs = mock_list.await_args.kwargs
    assert call_kwargs["status"] == "queued"
    assert len(result) == 1


# ---------------------------------------------------------------------------
# LE-001: CreateJobRequest payload default — no shared mutable state
# ---------------------------------------------------------------------------


def test_create_job_request_default_payload_is_empty_dict():
    """Default payload must be {} and not shared across instances."""
    req = CreateJobRequest(kind="card.generate")
    assert req.payload == {}


def test_create_job_request_payload_not_shared_between_instances():
    """Mutating one instance's payload must not affect another (no mutable default)."""
    req_a = CreateJobRequest(kind="card.generate")
    req_b = CreateJobRequest(kind="card.generate")
    req_a.payload["injected"] = True
    assert "injected" not in req_b.payload, (
        "Mutable default detected: req_b.payload was mutated via req_a"
    )


# ---------------------------------------------------------------------------
# LE-002: ownership check — str vs int normalisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_str_user_id_row_matches_int_caller():
    """Row user_id stored as str '1' must match int caller user_id=1 (LE-002 fix)."""
    mock_request = MagicMock()
    mock_pool = MagicMock()
    # asyncpg returns UUID/user_id columns as str; simulate that here
    row = {
        "id": "job-str",
        "user_id": "1",  # str from DB
        "status": "queued",
        "kind": "card.generate",
        "payload": {},
    }

    with patch.object(jobs_router.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        # user_id=1 (int) should match row["user_id"]="1" (str) — should NOT raise
        result = await jobs_router.get_job.__wrapped__(
            mock_request,
            job_id="job-str",
            user_id=1,
            db_pool=mock_pool,
        )

    assert result["id"] == "job-str"


@pytest.mark.asyncio
async def test_get_job_str_user_id_row_rejects_wrong_int_caller():
    """Row user_id='1' (str) must reject int caller user_id=2 (LE-002 fix)."""
    mock_request = MagicMock()
    mock_pool = MagicMock()
    row = {
        "id": "job-str2",
        "user_id": "1",  # str from DB
        "status": "queued",
        "kind": "card.generate",
        "payload": {},
    }

    with patch.object(jobs_router.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc_info:
            await jobs_router.get_job.__wrapped__(
                mock_request,
                job_id="job-str2",
                user_id=2,
                db_pool=mock_pool,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_job_str_user_id_row_matches_int_caller():
    """cancel_job: row user_id='5' (str) must allow int caller user_id=5 (LE-002 fix)."""
    mock_request = MagicMock()
    mock_pool = MagicMock()
    row = {
        "id": "job-cancel",
        "user_id": "5",  # str from DB
        "status": "queued",
        "kind": "card.generate",
        "payload": {},
        "source": "procrastinate",
    }
    prow = {"id": 99}

    mock_job_manager = AsyncMock()
    mock_job_manager.cancel_job_by_id_async = AsyncMock()
    mock_procrastinate_app = MagicMock()
    mock_procrastinate_app.job_manager = mock_job_manager

    with patch.object(jobs_router.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        with patch.object(
            jobs_router.jobs_lib,
            "get_procrastinate_job_for_jarvis_id",
            AsyncMock(return_value=prow),
        ):
            with patch("jarvis_common.task_registry.app", mock_procrastinate_app):
                result = await jobs_router.cancel_job.__wrapped__(
                    mock_request,
                    job_id="job-cancel",
                    user_id=5,
                    db_pool=mock_pool,
                )

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_cancel_job_str_user_id_row_rejects_wrong_int_caller():
    """cancel_job: row user_id='5' (str) must reject int caller user_id=9 (LE-002 fix)."""
    mock_request = MagicMock()
    mock_pool = MagicMock()
    row = {
        "id": "job-cancel2",
        "user_id": "5",  # str from DB
        "status": "queued",
        "kind": "card.generate",
        "payload": {},
        "source": "procrastinate",
    }

    with patch.object(jobs_router.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc_info:
            await jobs_router.cancel_job.__wrapped__(
                mock_request,
                job_id="job-cancel2",
                user_id=9,
                db_pool=mock_pool,
            )

    assert exc_info.value.status_code == 404


# B2-18: test_create_job_unsupported_kind_returns_400 removed — collapsed into
#   test_create_job_rejects_disallowed_kind parametrized above; "totally.unknown.kind"
#   is preserved as the second parametrize case.
