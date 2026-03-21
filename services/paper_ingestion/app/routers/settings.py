"""Settings, nudges, and source management endpoints."""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from jarvis_common import dynamic_update

from app.deps import limiter
from app.models import (
    ConfigEntry,
    NudgeResponse,
    NudgeUpdate,
    PapersBySourceItem,
    PapersByStatusItem,
    SourceResponse,
    SourceUpdate,
)
from app.services.litellm_config import ROLE_TO_ALIAS, reload_litellm, update_litellm_model

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["settings"])

_ALLOWED_CONFIG_KEYS = frozenset({
    "llm.smart_model", "llm.fast_model", "llm.embed_model",
    "ui.page_size",
    "ingestion.max_papers_per_run", "ingestion.chunk_size",
    "paper.max_daily", "paper.auto_generate_cards",
})

_NUDGE_ALLOWED_COLUMNS = frozenset({"cron_expression", "enabled"})
_NUDGE_JSONB_COLUMNS = frozenset()

_SOURCE_ALLOWED_COLUMNS = frozenset({"enabled", "priority", "config"})
_SOURCE_JSONB_COLUMNS = frozenset({"config"})


# --- User Config ---

@router.get("/config", response_model=list[ConfigEntry])
@limiter.limit("60/minute")
async def list_config(request: Request) -> list[ConfigEntry]:
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM user_config ORDER BY key")
    return [ConfigEntry(key=r["key"], value=r["value"]) for r in rows]


@router.get("/config/{key}")
@limiter.limit("60/minute")
async def get_config(request: Request, key: str) -> ConfigEntry:
    async with request.app.state.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT key, value FROM user_config WHERE key = $1", key)
    if not row:
        raise HTTPException(404, f"Config key '{key}' not found")
    return ConfigEntry(key=row["key"], value=row["value"])


@router.put("/config/{key}")
@limiter.limit("30/minute")
async def set_config(request: Request, key: str, body: ConfigEntry) -> ConfigEntry:
    if key not in _ALLOWED_CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")
    value_json = json.dumps(body.value)
    async with request.app.state.db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated_at = NOW()""",
            key, value_json,
        )
    if key in ROLE_TO_ALIAS:
        updated = update_litellm_model(key, body.value)
        if updated:
            await reload_litellm()
    return ConfigEntry(key=key, value=body.value)


# --- Scheduled Nudges ---

@router.get("/nudges", response_model=list[NudgeResponse])
@limiter.limit("60/minute")
async def list_nudges(request: Request) -> list[NudgeResponse]:
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM scheduled_nudges ORDER BY id")
    return [NudgeResponse(**dict(r)) for r in rows]


@router.put("/nudges/{nudge_id}", response_model=NudgeResponse)
@limiter.limit("30/minute")
async def update_nudge(request: Request, nudge_id: int, body: NudgeUpdate) -> NudgeResponse:
    async with request.app.state.db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM scheduled_nudges WHERE id = $1", nudge_id)
        if not existing:
            raise HTTPException(404, f"Nudge {nudge_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_NUDGE_ALLOWED_COLUMNS)
        if not updates:
            return NudgeResponse(**dict(existing))

        row = await dynamic_update(
            conn,
            "scheduled_nudges",
            nudge_id,
            updates,
            _NUDGE_ALLOWED_COLUMNS,
            jsonb_columns=_NUDGE_JSONB_COLUMNS,
        )
    return NudgeResponse(**dict(row))


# --- Paper Sources ---

@router.get("/sources", response_model=list[SourceResponse])
@limiter.limit("60/minute")
async def list_sources(request: Request) -> list[SourceResponse]:
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_sources ORDER BY id")
    return [SourceResponse(**dict(r)) for r in rows]


@router.put("/sources/{source_id}", response_model=SourceResponse)
@limiter.limit("30/minute")
async def update_source(request: Request, source_id: int, body: SourceUpdate) -> SourceResponse:
    async with request.app.state.db_pool.acquire() as conn:
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


# --- Analytics ---

@router.get("/analytics/papers-by-source", response_model=list[PapersBySourceItem])
@limiter.limit("60/minute")
async def papers_by_source(request: Request) -> list[dict]:
    """Return paper counts grouped by source type."""
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_type, COUNT(*) AS count FROM papers GROUP BY source_type ORDER BY count DESC"
        )
    return [{"source_type": r["source_type"], "count": r["count"]} for r in rows]


@router.get("/analytics/papers-by-status", response_model=list[PapersByStatusItem])
@limiter.limit("60/minute")
async def papers_by_status(request: Request) -> list[dict]:
    """Return paper counts grouped by user-state status."""
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(pus.status, 'new') AS status, COUNT(*) AS count
            FROM papers p
            LEFT JOIN paper_user_state pus ON p.id = pus.paper_id
            GROUP BY COALESCE(pus.status, 'new')
            ORDER BY count DESC
            """
        )
    return [{"status": r["status"], "count": r["count"]} for r in rows]
