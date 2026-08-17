"""Administrative surface for Platform-owned account erasure."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common.auth import require_admin
from pydantic import BaseModel

from platform_api.repos.erasure import ErasureState
from platform_api.services.erasure import ErasureNotEligibleError, process_request

router = APIRouter(prefix="/api/admin/erasure", tags=["erasure"])


class ErasureStatus(BaseModel):
    """Stable state returned for a durable erasure request."""

    request_id: uuid.UUID
    state: ErasureState


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


__all__ = ["router"]
