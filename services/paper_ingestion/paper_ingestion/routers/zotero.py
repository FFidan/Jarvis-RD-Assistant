"""Zotero integration endpoints."""

from __future__ import annotations

import logging

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import jobs as jobs_lib

from paper_ingestion.deps import get_db_pool, get_http_client, limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["zotero"])


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

    cfg = await _get_zotero_config(db_pool)
    if not cfg.get("enabled"):
        return {"ok": False, "detail": "Zotero integration is disabled"}

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")

    if not api_key or not user_id:
        return {"ok": False, "detail": "Zotero API key or user ID not configured"}

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),
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
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM papers WHERE id = $1", paper_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Paper not found")

    job_id = await jobs_lib.enqueue(db_pool, "zotero.push", {"paper_id": paper_id})
    return {"job_id": job_id, "status": "queued"}


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
    async with db_pool.acquire() as conn:
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
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM papers WHERE id = $1", paper_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Paper not found")

    job_id = await jobs_lib.enqueue(db_pool, "zotero.resync", {"paper_id": paper_id})
    return {"job_id": job_id, "status": "queued"}
