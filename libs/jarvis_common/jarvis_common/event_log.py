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
    """Persist a structured event into ``system_events``. Never raises.

    On DB outage the exception is swallowed and a ``WARNING`` is emitted so
    callers do not need to guard every call site with ``try/except``.

    Parameters
    ----------
    pool:
        asyncpg connection pool.
    level:
        Severity level for the event row.
    category:
        Semantic category.  ``'infra'`` is reserved for Vector sidecar use.
    source:
        Module or subsystem that produced the event (e.g. ``"auth"``,
        ``"pulse"``, ``"zotero"``).
    message:
        Short, machine-readable event identifier
        (e.g. ``"magic_link_dev_mode"``, ``"invalid_api_key"``).
    context:
        Optional free-form JSONB payload.  Defaults to ``{}`` when ``None``.
    correlation_id:
        UUID carried from :data:`jarvis_common.logging_config.correlation_id_var`
        when ``None``; callers may supply an explicit value to override.

    """
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
