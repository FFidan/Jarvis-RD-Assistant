"""Zotero integration endpoints."""

from __future__ import annotations

import logging
import uuid

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership, current_user_id_or_none
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, get_http_client, limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["zotero"])


class JobEnqueuedResponse(BaseModel):
    """Generic response for endpoints that enqueue a background job."""

    job_id: str
    status: str


# ---------------------------------------------------------------------------
# POST /api/zotero/test
# ---------------------------------------------------------------------------


@router.post("/zotero/test")
@limiter.limit("10/minute")
async def test_zotero_connection(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict:
    """Test Zotero credentials from user_config.

    Returns ``{"ok": true}`` if the stored API key + user_id are valid,
    ``{"ok": false, "detail": "..."}`` otherwise.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient
    from paper_ingestion.integrations.zotero_service import _get_zotero_config

    user_id_for_config = await current_user_id_or_none(request)
    cfg = await _get_zotero_config(db_pool, user_id=user_id_for_config)

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None

    if not api_key or not user_id:
        return {"ok": False, "detail": "Zotero API key or user ID not configured"}

    if library_type == "group" and group_id is None:
        return {"ok": False, "detail": "Group ID is required when library type is 'group'"}

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),  # type: ignore[arg-type]
        group_id=group_id,
        http_client=http_client,
    )
    ok = await client.test_connection()
    if ok:
        return {"ok": True}
    return {"ok": False, "detail": "Zotero API returned an error — check API key and user ID"}


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/zotero  — enqueue push
# ---------------------------------------------------------------------------


@router.post("/papers/{paper_id}/zotero", status_code=202)
@limiter.limit("30/minute")
async def push_paper_to_zotero(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Enqueue a zotero.push job for the given paper.

    Returns ``{"job_id": "...", "status": "queued"}``.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM papers WHERE id = $1", paper_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Paper not found")
        await assert_paper_ownership(conn, paper_id, user_id)

    from jarvis_common.task_registry import KIND_TO_TASK

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["zotero.push"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_id=paper_id
    )
    return {"job_id": jarvis_job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# GET /api/papers/{paper_id}/zotero  — fetch Zotero state
# ---------------------------------------------------------------------------


@router.get("/papers/{paper_id}/zotero")
@limiter.limit("60/minute")
async def get_paper_zotero_state(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Return the Zotero sync state for a paper.

    Returns ``{"zotero_item_key", "zotero_citation_key", "zotero_last_pushed_at"}``
    (all fields may be ``null`` if the paper has not been pushed).
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow(
            "SELECT zotero_item_key, zotero_citation_key, zotero_last_pushed_at"
            " FROM papers WHERE id = $1",
            paper_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")

    return {
        "paper_id": paper_id,
        "zotero_item_key": row["zotero_item_key"],
        "zotero_citation_key": row["zotero_citation_key"],
        "zotero_last_pushed_at": (
            row["zotero_last_pushed_at"].isoformat() if row["zotero_last_pushed_at"] else None
        ),
    }


# ---------------------------------------------------------------------------
# POST /api/zotero/resync/{paper_id}  — enqueue resync
# ---------------------------------------------------------------------------


@router.post("/zotero/resync/{paper_id}", status_code=202)
@limiter.limit("10/minute")
async def resync_paper_to_zotero(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Enqueue a zotero.resync job for the given paper (force re-push).

    Returns ``{"job_id": "...", "status": "queued"}``.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM papers WHERE id = $1", paper_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Paper not found")
        await assert_paper_ownership(conn, paper_id, user_id)

    from jarvis_common.task_registry import KIND_TO_TASK

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["zotero.resync"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_id=paper_id
    )
    return {"job_id": jarvis_job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# POST /api/zotero/sync-annotations/{paper_id}  — enqueue annotation import
# ---------------------------------------------------------------------------


@router.post(
    "/zotero/sync-annotations/{paper_id}",
    status_code=202,
    response_model=JobEnqueuedResponse,
)
@limiter.limit("10/minute")
async def sync_annotations_for_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JobEnqueuedResponse:
    """Enqueue Zotero annotation import for a linked paper."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM papers WHERE id = $1", paper_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Paper not found")
        await assert_paper_ownership(conn, paper_id, user_id)

    from jarvis_common.task_registry import KIND_TO_TASK

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["zotero.sync_annotations"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_id=paper_id
    )
    return JobEnqueuedResponse(job_id=jarvis_job_id, status="queued")


# ---------------------------------------------------------------------------
# POST /api/zotero/poll  — trigger manual Zotero sync
# ---------------------------------------------------------------------------


@router.post("/zotero/poll", response_model=JobEnqueuedResponse)
@limiter.limit("6/hour")
async def poll_now(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JobEnqueuedResponse:
    """Trigger manual Zotero sync — enqueues a ``zotero.sync_from_zotero`` job.

    Returns immediately with a ``job_id`` so the caller can poll
    ``GET /api/jobs/{job_id}`` for progress.  Rate-limited to 6/hour.
    """
    from jarvis_common.task_registry import KIND_TO_TASK

    logger.info("zotero.poll: enqueueing sync job")
    user_id = await current_user_id_or_none(request)
    jarvis_job_id = str(uuid.uuid4())
    # Phase 2 WS-2D: thread caller user_id so per-user paper attribution works
    # in multi-user mode. (Pre-WS-2A this was annotated `system-wide cron` but
    # this is an interactive user-triggered poll, not a cron — audit reclassify.)
    await KIND_TO_TASK["zotero.sync_from_zotero"].defer_async(job_id=jarvis_job_id, user_id=user_id)
    return JobEnqueuedResponse(job_id=jarvis_job_id, status="queued")
