"""Owner-local Research enqueue command for the unified jobs facade.

Platform owns the browser-facing jobs API. This router accepts one signed
Platform dispatch command and owns the Research task-registry boundary.

Internal-only kinds (``paper.download``, ``papers.scan_local``,
``extraction.single``, ``citations.batch_fetch``, ``digest.weekly``,
``paper.summarize``) are deliberately excluded from the public allowlist —
they are only triggered by the service itself.

Per-kind payloads are validated through a Pydantic discriminated union, so
unknown kinds and missing / wrong-typed required fields are rejected with
HTTP 422 before the handler runs.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership, assert_papers_ownership
from jarvis_common.job_contracts import (
    RESEARCH_PAYLOAD_SCHEMAS,
    RESEARCH_PUBLIC_JOB_KINDS,
    ExtractionBatchPayload,
    NoopTestPayload,
    PaperAnalyzePayload,
    PaperProcessPayload,
    PapersBatchProcessPayload,
    PapersBatchSummarizePayload,
    PulseGeneratePayload,
    paper_ids_for_payload,
)
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool

# ---------------------------------------------------------------------------
# Per-kind payload schemas shared with the Platform facade.
# ---------------------------------------------------------------------------


PI_PAYLOAD_SCHEMAS = RESEARCH_PAYLOAD_SCHEMAS
PI_PUBLIC_JOB_KINDS = RESEARCH_PUBLIC_JOB_KINDS
_extract_paper_ids = paper_ids_for_payload
router = APIRouter(prefix="/api/jobs", tags=["jobs"])
type DatabasePool = Annotated[asyncpg.Pool, Depends(get_db_pool)]


class OwnerDispatchRequest(BaseModel):
    """Platform-authorized enqueue request for one Research-owned job."""

    kind: str
    payload: dict[str, Any]


@router.post("/dispatch", status_code=202)
async def dispatch_owner_job(
    body: OwnerDispatchRequest,
    request: Request,
    db_pool: DatabasePool,
) -> dict[str, str]:
    """Authorize and enqueue a Research job for the signed Platform subject."""
    user_id = getattr(request.state, "user_id", None)
    if getattr(request.state, "identity_principal", None) != "platform" or not isinstance(
        user_id, int
    ):
        raise HTTPException(status_code=403, detail="Job dispatch is forbidden")
    if body.kind not in PI_PUBLIC_JOB_KINDS:
        raise HTTPException(status_code=400, detail="Research job kind is not allowed")
    schema = PI_PAYLOAD_SCHEMAS[body.kind]
    payload = schema.model_validate({**body.payload, "kind": body.kind}).model_dump(
        exclude={"kind"}
    )
    paper_ids = _extract_paper_ids(payload)
    if isinstance(paper_ids, int):
        async with db_pool.acquire() as conn:
            await assert_paper_ownership(conn, paper_ids, user_id)
    elif isinstance(paper_ids, list):
        async with db_pool.acquire() as conn:
            await assert_papers_ownership(conn, paper_ids, user_id)
    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    task = KIND_TO_TASK.get(body.kind)
    if task is None:
        raise HTTPException(status_code=503, detail="Research job dispatch is unavailable")
    job_id = str(uuid.uuid4())
    await task.defer_async(job_id=job_id, user_id=user_id, **payload)
    return {"job_id": job_id, "status": "queued"}


__all__ = [
    "PI_PUBLIC_JOB_KINDS",
    "PI_PAYLOAD_SCHEMAS",
    "PulseGeneratePayload",
    "PaperProcessPayload",
    "PaperAnalyzePayload",
    "PapersBatchProcessPayload",
    "PapersBatchSummarizePayload",
    "ExtractionBatchPayload",
    "NoopTestPayload",
    "router",
]
