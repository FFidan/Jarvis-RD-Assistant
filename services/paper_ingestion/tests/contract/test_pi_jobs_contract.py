"""Research-owned jobs command boundary contracts.

Platform is the sole browser-facing ``/api/jobs`` facade.  Research exposes
only the signed, owner-local enqueue command at ``/api/jobs/dispatch``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from paper_ingestion.routers.jobs import OwnerDispatchRequest, dispatch_owner_job, router


pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


def _owner_request(user_id: int | None, principal: str | None) -> SimpleNamespace:
    """Build the only request state the owner command consumes."""
    return SimpleNamespace(state=SimpleNamespace(user_id=user_id, identity_principal=principal))


def _pool() -> MagicMock:
    """Return a pool whose acquire context has a harmless connection."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


async def test_research_exposes_only_owner_dispatch_route() -> None:
    """Research has no browser-facing unified jobs facade."""
    assert {route.path for route in router.routes} == {"/api/jobs/dispatch"}


async def test_owner_dispatch_rejects_unsigned_or_wrong_principal() -> None:
    """Only a signed Platform principal may enqueue a Research job."""
    body = OwnerDispatchRequest(kind="paper.process", payload={"paper_id": 41})
    with pytest.raises(HTTPException, match="forbidden") as exc_info:
        await dispatch_owner_job(body, _owner_request(7, "research"), _pool())
    assert exc_info.value.status_code == 403


async def test_owner_dispatch_validates_ownership_and_defers_exact_kind() -> None:
    """The owner command validates paper scope before deferring its task."""
    task = MagicMock(defer_async=AsyncMock())
    ownership = AsyncMock()
    with (
        patch("paper_ingestion.routers.jobs.assert_paper_ownership", ownership),
        patch.dict("jarvis_common.task_registry._TASK_MAP", {"paper.process": task}),
    ):
        response = await dispatch_owner_job(
            OwnerDispatchRequest(kind="paper.process", payload={"paper_id": 41}),
            _owner_request(7, "platform"),
            _pool(),
        )

    assert response["status"] == "queued"
    assert response["job_id"]
    ownership.assert_awaited_once()
    task.defer_async.assert_awaited_once()
    assert task.defer_async.call_args.kwargs == {
        "job_id": response["job_id"],
        "user_id": 7,
        "paper_id": 41,
        "force": False,
    }


async def test_owner_dispatch_refuses_a_learning_kind() -> None:
    """Research cannot enqueue work owned by Learning."""
    with pytest.raises(HTTPException, match="not allowed") as exc_info:
        await dispatch_owner_job(
            OwnerDispatchRequest(kind="card.generate", payload={}),
            _owner_request(7, "platform"),
            _pool(),
        )
    assert exc_info.value.status_code == 400
