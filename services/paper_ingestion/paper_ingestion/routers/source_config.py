"""Admin endpoints for per-source API configuration and cooldown management.

Endpoints
---------
PATCH  /api/settings/sources/{source_type}
    Update a source's ``config`` JSONB (merge; does not clobber other keys).
    Validates ``source_type`` against the registered source registry.
    Admin session required.

POST   /api/settings/sources/{source_type}/clear-cooldown
    Reset the ``source_health`` row for a source: clear ``cooldown_until``,
    set ``last_status = 'ok'``, zero ``consecutive_failures``.
    Admin session required.

Note: clear-cooldown uses direct SQL (``UPDATE source_health``) because
``PersistentSourceRateLimiter`` has no ``reset()`` method as of this writing.
When B4/B5 sibling task adds ``reset()`` to the shared limiter, switch to:
    ``await limiter.reset()`` from ``jarvis_common.source_rate_limiter``.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from jarvis_common.auth import require_admin, verify_api_key
from pydantic import BaseModel, Field

from paper_ingestion.deps import get_db_pool
from paper_ingestion.sources.registry import get_all_source_types, get_source_class

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settings/sources",
    tags=["source-config"],
    dependencies=[Depends(verify_api_key), Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SourceConfigBody(BaseModel):
    """Body for PATCH /api/settings/sources/{source_type}."""

    api_key: Annotated[str | None, Field(default=None, max_length=512)]
    email: Annotated[str | None, Field(default=None, max_length=320)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_source_type(source_type: str) -> None:
    """Raise 400/404 for an unknown source_type.

    Uses the live registry so new sources added via ``@register_source``
    are automatically accepted without touching this router.
    """
    if get_source_class(source_type) is None:
        known = ", ".join(sorted(get_all_source_types()))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown source type '{source_type}'. Known: {known}",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.patch("/{source_type}")
async def update_source_config(
    source_type: str,
    body: SourceConfigBody,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, bool]:
    """Merge ``api_key`` / ``email`` into ``paper_sources.config`` for *source_type*.

    Only the keys present in the request body are written; other keys in the
    ``config`` JSONB column are preserved.  Raises 404 for unregistered source
    types and 400 when neither field is supplied.
    """
    _validate_source_type(source_type)

    updates: dict[str, Any] = {}
    if body.api_key is not None:
        updates["api_key"] = body.api_key
    if body.email is not None:
        updates["email"] = body.email

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of 'api_key' or 'email' must be provided.",
        )

    # Merge into the existing config JSONB using the || operator so keys not
    # mentioned in the request are preserved.
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE paper_sources
               SET config = COALESCE(config, '{}'::jsonb) || $1::jsonb
             WHERE source_type = $2
            """,
            json.dumps(updates),
            source_type,
        )

    if result == "UPDATE 0":
        # Row is absent — insert it so sources that start with no row work too.
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO paper_sources (source_type, enabled, config)
                VALUES ($1, FALSE, $2::jsonb)
                ON CONFLICT (source_type) DO UPDATE
                   SET config = paper_sources.config || EXCLUDED.config
                """,
                source_type,
                json.dumps(updates),
            )

    logger.info("source_config: updated %s config keys=%s", source_type, list(updates))
    return {"ok": True}


@router.post("/{source_type}/clear-cooldown")
async def clear_source_cooldown(
    source_type: str,
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, bool]:
    """Reset the ``source_health`` cooldown for *source_type*.

    Clears ``cooldown_until``, sets ``last_status = 'ok'``, and zeroes
    ``consecutive_failures`` for every ``source_health`` row matching this
    source type (both global rows with ``user_id IS NULL`` and per-user rows).

    NOTE: This endpoint implements the reset via a direct idempotent
    ``UPDATE source_health`` rather than ``PersistentSourceRateLimiter.reset()``
    because that method does not yet exist.  When a shared ``reset()`` method
    is added to ``jarvis_common.source_rate_limiter.PersistentSourceRateLimiter``
    (anticipated in Wave-1 sibling task), this endpoint should delegate to it
    and remove the inline SQL.
    """
    _validate_source_type(source_type)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE source_health
               SET last_status         = 'ok',
                   cooldown_until      = NULL,
                   consecutive_failures = 0,
                   updated_at          = NOW()
             WHERE source_type = $1
            """,
            source_type,
        )

    caller_id = getattr(request.state, "user_id", None)
    logger.info(
        "source_config: clear-cooldown source_type=%s by user_id=%s",
        source_type,
        caller_id,
    )
    return {"ok": True}


__all__ = ["router"]
