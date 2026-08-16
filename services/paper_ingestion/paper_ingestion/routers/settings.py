"""Research analytics, source management, and data-export routes.

HTTP handlers remain thin and delegate owner-local behavior to service modules.
Provider configuration and operator controls are owned by the Platform API.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from jarvis_common.auth import current_user_id_strict

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import PapersBySourceItem, PapersByStatusItem
from paper_ingestion.routers.settings_sources import sources_router
from paper_ingestion.services.analytics_queries import (
    fetch_papers_by_source,
    fetch_papers_by_status,
)
from paper_ingestion.services.data_export import build_export_zip

router = APIRouter(prefix="/api", tags=["settings"])
router.include_router(sources_router)


@router.get("/analytics/papers-by-source", response_model=list[PapersBySourceItem])
@limiter.limit("60/minute")
async def papers_by_source(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[dict[str, Any]]:
    """Return paper counts grouped by source type for the current user.

    Parameters
    ----------
    request : Request
        Authenticated Research request carrying the caller role.
    db_pool : asyncpg.Pool
        Research database pool.
    user_id : int
        Authenticated user identifier.

    Returns
    -------
    list[dict[str, Any]]
        Source names and visible paper counts.
    """
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        return await fetch_papers_by_source(conn, user_id, is_admin=is_admin)


@router.get("/analytics/papers-by-status", response_model=list[PapersByStatusItem])
@limiter.limit("60/minute")
async def papers_by_status(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[dict[str, Any]]:
    """Return paper counts grouped by workflow status for the current user.

    Parameters
    ----------
    request : Request
        Authenticated Research request carrying the caller role.
    db_pool : asyncpg.Pool
        Research database pool.
    user_id : int
        Authenticated user identifier.

    Returns
    -------
    list[dict[str, Any]]
        Workflow statuses and visible paper counts.
    """
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        return await fetch_papers_by_status(conn, user_id, is_admin=is_admin)


@router.get("/me/export")
@limiter.limit("5/minute")
async def export_my_data(
    request: Request,
    caller_user_id: int = Depends(current_user_id_strict),
) -> StreamingResponse:
    """Stream a ZIP archive of the calling user's structured Research data.

    Parameters
    ----------
    request : Request
        Authenticated Research request whose application owns the database pool.
    caller_user_id : int
        Authenticated user identifier.

    Returns
    -------
    StreamingResponse
        ZIP archive containing JSON exports without PDF or embedding binaries.
    """
    data = await build_export_zip(request.app.state.db_pool, caller_user_id)
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="jarvis-data-export.zip"'},
    )


__all__ = ["export_my_data", "papers_by_source", "papers_by_status", "router"]
