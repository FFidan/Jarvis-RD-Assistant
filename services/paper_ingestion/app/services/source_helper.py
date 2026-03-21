"""Source plugin helper — resolves a PaperSource instance for a given type."""

import asyncpg
import httpx
from fastapi import HTTPException, Request

from app.models import PaperSourceConfig, SourceType
from app.sources.registry import get_source_class


async def get_source_for_type(
    source_type: SourceType,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    request: "Request | None" = None,
):
    """Return a PaperSource for the given type.

    C-8: Returns the pre-initialized singleton from app.state.sources when
    available so the per-instance rate limiter persists across requests.
    Falls back to creating a new instance (e.g. for sources not pre-loaded).

    Note: Cached singletons preserve rate limiter state across requests.
    Config fields (api_key, etc.) are read at startup -- changes via Settings
    UI require a service restart to take effect.
    """
    # Return cached singleton if available
    if request is not None:
        cached = getattr(request.app.state, "sources", {}).get(source_type.value)
        if cached is not None:
            # Still validate enabled status from DB before returning
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT enabled FROM paper_sources WHERE source_type = $1",
                    source_type.value,
                )
            if not row:
                raise HTTPException(
                    status_code=404, detail=f"Source '{source_type}' not in database"
                )
            if not row["enabled"]:
                raise HTTPException(status_code=400, detail=f"Source '{source_type}' is disabled")
            return cached

    # Fallback: create on demand (for sources not pre-initialized)
    source_cls = get_source_class(source_type.value)
    if not source_cls:
        raise HTTPException(status_code=404, detail=f"Source type '{source_type}' not registered")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, source_type, enabled, config FROM paper_sources WHERE source_type = $1",
            source_type.value,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' not in database")
    if not row["enabled"]:
        raise HTTPException(status_code=400, detail=f"Source '{source_type}' is disabled")

    config = PaperSourceConfig(
        id=row["id"],
        source_type=row["source_type"],
        enabled=row["enabled"],
        config=row["config"] or {},
    )
    return source_cls(config, http_client)


# Backward-compatible alias while internal imports are migrated.
_get_source = get_source_for_type
