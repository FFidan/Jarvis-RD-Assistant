"""Explicit semantic event logging into system_events."""

import logging
import uuid
from typing import Literal

import asyncpg

from .logging_config import correlation_id_var

logger = logging.getLogger(__name__)


async def log_event(
    *,
    pool: asyncpg.Pool,
    level: Literal["debug", "info", "warning", "error", "critical"],
    category: Literal["error", "job", "source", "auth", "config"],  # NOT 'infra' — Vector-only
    source: str,
    message: str,
    context: dict | None = None,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Persist a semantic event. Idempotent on DB outage (logs warning, doesn't raise)."""
    if correlation_id is None:
        correlation_id = correlation_id_var.get()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO system_events"
                " (level, category, source, message, context, correlation_id)"
                " VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
                level,
                category,
                source,
                message,
                context or {},
                correlation_id,
            )
    except (asyncpg.PostgresError, OSError) as exc:
        logger.warning("log_event failed: %s", exc)
