"""Platform-owned public facade for unified background jobs."""

from __future__ import annotations

import uuid
from typing import Any, cast

import httpx
from fastapi import HTTPException, Request
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.job_contracts import PUBLIC_JOB_KINDS, PUBLIC_PAYLOAD_SCHEMAS
from jarvis_common.jobs import queue_for_kind
from jarvis_common.jobs_router import build_jobs_router

from platform_api.config import get_platform_settings
from platform_api.deps import get_db_pool, limiter


async def _dispatch_to_owner(
    kind: str, payload: dict[str, Any], user_id: int, request: Request
) -> str:
    """Send one signed enqueue command to the exact queue-owning service."""
    try:
        owner = queue_for_kind(kind)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="Job kind is not allowed") from exc
    audience = "research" if owner == "paper_ingestion" else "learning"
    origin = (
        get_platform_settings().research_api_url
        if audience == "research"
        else get_platform_settings().learning_api_url
    )
    path = "/api/jobs/dispatch"
    request_id = str(uuid.uuid4())
    signer = getattr(request.app.state, "identity_signer", None)
    if not isinstance(signer, IdentityAssertionSigner):
        raise HTTPException(status_code=503, detail="Job dispatch is temporarily unavailable")
    signer = cast(IdentityAssertionSigner, signer)
    assertion = signer.issue(
        audience=audience,
        subject=f"user:{user_id}",
        principal="platform",
        user_id=user_id,
        request_id=request_id,
        request_method="POST",
        request_path=path,
        scopes=(f"{audience}:jobs:write",),
    )
    client = getattr(request.app.state, "http_client", None)
    if not isinstance(client, httpx.AsyncClient):
        raise HTTPException(status_code=503, detail="Job dispatch is temporarily unavailable")
    try:
        response = await client.post(
            f"{origin}{path}",
            headers={"X-Jarvis-Identity": assertion, "X-Request-Id": request_id},
            json={"kind": kind, "payload": payload},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail="Job dispatch is temporarily unavailable"
        ) from exc
    if response.status_code >= 500:
        raise HTTPException(status_code=503, detail="Job dispatch is temporarily unavailable")
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail="Job request was rejected")
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=503, detail="Job dispatch is temporarily unavailable"
        ) from exc
    job_id = body.get("job_id") if isinstance(body, dict) else None
    if not isinstance(job_id, str) or not job_id:
        raise HTTPException(status_code=503, detail="Job dispatch is temporarily unavailable")
    return job_id


router = build_jobs_router(
    service_name="platform_api",
    public_kinds=PUBLIC_JOB_KINDS,
    get_db_pool=get_db_pool,
    limiter=limiter,
    payload_schemas=PUBLIC_PAYLOAD_SCHEMAS,
    dispatch_job=lambda request, kind, payload, user_id: _dispatch_to_owner(
        kind, payload, user_id, request
    ),
)

__all__ = ["router"]
