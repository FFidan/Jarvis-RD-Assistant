"""Administrative surface for Platform-owned account erasure."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common.auth import require_admin
from pydantic import BaseModel

from platform_api.repos import erasure
from platform_api.repos.erasure import ErasureState
from platform_api.services.erasure import ErasureNotEligibleError, process_request

router = APIRouter(prefix="/api/admin/erasure", tags=["erasure"])


class ErasureStatus(BaseModel):
    """Stable state returned for a durable erasure request."""

    request_id: uuid.UUID
    state: ErasureState


class ErasureRecord(BaseModel):
    """One durable erasure request as an operator needs to read it."""

    request_id: uuid.UUID
    user_id: int
    state: ErasureState
    attempts: int
    resume_state: ErasureState
    last_error: str | None
    next_attempt_at: datetime
    requested_at: datetime


@router.get("", response_model=list[ErasureRecord], dependencies=[Depends(require_admin)])
async def list_erasure_requests(request: Request) -> list[ErasureRecord]:
    """List durable erasure requests, most recently requested first.

    A request that stranded records why in ``last_error``; without this view
    that reason, and the stranding itself, are invisible to the operator who
    has to decide whether to resume it.
    """
    rows = await erasure.list_requests(request.app.state.db_pool)
    return [ErasureRecord.model_validate(dict(row)) for row in rows]


@router.post(
    "/{request_id}/process",
    response_model=ErasureStatus,
    dependencies=[Depends(require_admin)],
)
async def process_erasure(request_id: uuid.UUID, request: Request) -> ErasureStatus:
    """Run one bounded owner-command pass for an eligible erasure request."""
    try:
        state = await process_request(request.app, request_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Erasure request not found") from exc
    except ErasureNotEligibleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ErasureStatus(request_id=request_id, state=state)


@router.post(
    "/{request_id}/resume",
    response_model=ErasureStatus,
    dependencies=[Depends(require_admin)],
)
async def resume_erasure(request_id: uuid.UUID, request: Request) -> ErasureStatus:
    """Return a stranded erasure request to its recorded phase.

    A request strands only after every retry failed, so resuming it also clears
    the exhausted attempt budget; without that it would strand again on its
    first failure, with no retry recorded.
    """
    try:
        state = await erasure.resume(request.app.state.db_pool, request_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Erasure request not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ErasureStatus(request_id=request_id, state=state)


__all__ = ["router"]
