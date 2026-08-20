"""Owner-local Learning enqueue command for the unified jobs facade.

Platform owns the browser-facing jobs API. This router accepts one signed
Platform dispatch command and owns the Learning task-registry boundary.

``card.generate_batch`` is intentionally excluded from the public allowlist;
that batch operation is dispatched through ``POST /api/generate/batch`` with
its own validation.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership
from jarvis_common.job_contracts import (
    LEARNING_PAYLOAD_SCHEMAS,
    LEARNING_PUBLIC_JOB_KINDS,
    CardGeneratePayload,
    paper_ids_for_payload,
)
from pydantic import BaseModel

from learning_engine.deps import get_db_pool

# ---------------------------------------------------------------------------
# Allowlist of job kinds Platform may dispatch to Learning.
# ---------------------------------------------------------------------------
LE_PUBLIC_JOB_KINDS = LEARNING_PUBLIC_JOB_KINDS
_CardGeneratePayload = CardGeneratePayload
_card_generate_paper_extractor = paper_ids_for_payload
router = APIRouter(prefix="/api/jobs", tags=["jobs"])
type DatabasePool = Annotated[asyncpg.Pool, Depends(get_db_pool)]


class OwnerDispatchRequest(BaseModel):
    """Platform-authorized enqueue request for one Learning-owned job."""

    kind: str
    payload: dict[str, Any]


@router.post("/dispatch", status_code=202)
async def dispatch_owner_job(
    body: OwnerDispatchRequest,
    request: Request,
    db_pool: DatabasePool,
) -> dict[str, str]:
    """Authorize and enqueue a Learning job for the signed Platform subject."""
    user_id = getattr(request.state, "user_id", None)
    if getattr(request.state, "identity_principal", None) != "platform" or not isinstance(
        user_id, int
    ):
        raise HTTPException(status_code=403, detail="Job dispatch is forbidden")
    if body.kind not in LE_PUBLIC_JOB_KINDS:
        raise HTTPException(status_code=400, detail="Learning job kind is not allowed")
    payload = (
        LEARNING_PAYLOAD_SCHEMAS[body.kind]
        .model_validate({**body.payload, "kind": body.kind})
        .model_dump(exclude={"kind"})
    )
    paper_id = _card_generate_paper_extractor(payload)
    if not isinstance(paper_id, int):
        raise HTTPException(status_code=422, detail="paper_id is required")
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    task = KIND_TO_TASK.get(body.kind)
    if task is None:
        raise HTTPException(status_code=503, detail="Learning job dispatch is unavailable")
    job_id = str(uuid.uuid4())
    await task.defer_async(job_id=job_id, user_id=user_id, **payload)
    return {"job_id": job_id, "status": "queued"}


__all__ = [
    "LE_PUBLIC_JOB_KINDS",
    "router",
]
