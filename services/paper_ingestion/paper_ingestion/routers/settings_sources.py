"""Nudge and paper-source management endpoints — sub-router for /api/nudges/* and /api/sources/*.

Thin transport layer: parse → auth check → delegate to the owning service module.

Included by ``paper_ingestion.routers.settings.router`` via
``router.include_router(sources_router)``.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import dynamic_update
from jarvis_common.auth import require_admin
from jarvis_common.config_metadata import (
    _NUDGE_ALLOWED_COLUMNS,
    _SOURCE_ALLOWED_COLUMNS,
    _SOURCE_JSONB_COLUMNS,
)
from jarvis_common.config_validators import _validate_zotero_cron
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import (
    NudgeResponse,
    NudgeUpdate,
    SourceResponse,
    SourceUpdate,
)

logger = logging.getLogger(__name__)
sources_router = APIRouter()


# ---------------------------------------------------------------------------
# Request models (router-local)
# ---------------------------------------------------------------------------


class ReorderRequest(BaseModel):
    source_types: list[str]


# ---------------------------------------------------------------------------
# Scheduled Nudges
# ---------------------------------------------------------------------------


@sources_router.get(
    "/nudges", response_model=list[NudgeResponse], dependencies=[Depends(require_admin)]
)
@limiter.limit("60/minute")
async def list_nudges(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[NudgeResponse]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM scheduled_nudges ORDER BY id")
    return [NudgeResponse(**dict(r)) for r in rows]


@sources_router.put("/nudges/{nudge_id}", response_model=NudgeResponse)
@limiter.limit("30/minute")
async def update_nudge(
    request: Request,
    nudge_id: int,
    body: NudgeUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> NudgeResponse:
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM scheduled_nudges WHERE id = $1", nudge_id)
        if not existing:
            raise HTTPException(404, f"Nudge {nudge_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_NUDGE_ALLOWED_COLUMNS)
        if not updates:
            return NudgeResponse(**dict(existing))

        if "cron_expression" in updates:
            try:
                _validate_zotero_cron(updates["cron_expression"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        row = await conn.fetchrow(
            """
            SELECT * FROM learning.update_scheduled_nudge_v1(
                $1, $2, $3, $4, $5, $6, $7::jsonb
            )
            """,
            nudge_id,
            "cron_expression" in updates,
            updates.get("cron_expression"),
            "enabled" in updates,
            updates.get("enabled"),
            "config" in updates,
            updates.get("config"),
        )

        if row is None:
            raise HTTPException(404, f"Nudge {nudge_id} not found")

    return NudgeResponse(**dict(row))


# ---------------------------------------------------------------------------
# Paper Sources
# ---------------------------------------------------------------------------


@sources_router.get("/sources", response_model=list[SourceResponse])
@limiter.limit("60/minute")
async def list_sources(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> list[SourceResponse]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_sources ORDER BY display_order ASC, id ASC")
    return [SourceResponse(**dict(r)) for r in rows]


@sources_router.patch("/sources/reorder", response_model=list[SourceResponse])
@limiter.limit("10/minute")
async def reorder_sources(
    request: Request,
    body: ReorderRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> list[SourceResponse]:
    """Persist UI drag-and-drop order by assigning display_order = position index."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT source_type FROM paper_sources")
    existing = {r["source_type"] for r in rows}
    missing = set(body.source_types) - existing
    if missing:
        raise HTTPException(400, detail=f"Unknown sources: {sorted(missing)}")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for idx, stype in enumerate(body.source_types, start=1):
                await conn.execute(
                    "UPDATE paper_sources SET display_order = $1 WHERE source_type = $2",
                    idx,
                    stype,
                )
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_sources ORDER BY display_order ASC, id ASC")
    return [SourceResponse(**dict(r)) for r in rows]


@sources_router.put("/sources/{source_id}", response_model=SourceResponse)
@limiter.limit("30/minute")
async def update_source(
    request: Request,
    source_id: int,
    body: SourceUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> SourceResponse:
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM paper_sources WHERE id = $1", source_id)
        if not existing:
            raise HTTPException(404, f"Source {source_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_SOURCE_ALLOWED_COLUMNS)
        if not updates:
            return SourceResponse(**dict(existing))

        row = await dynamic_update(
            conn,
            "paper_sources",
            source_id,
            updates,
            _SOURCE_ALLOWED_COLUMNS,
            jsonb_columns=_SOURCE_JSONB_COLUMNS,
        )
    return SourceResponse(**dict(row))
