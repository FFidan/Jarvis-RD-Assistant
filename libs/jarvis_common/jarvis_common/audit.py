"""Audit log helper: append security and destructive-mutation events."""

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


async def log_audit(
    pool: asyncpg.Pool,
    *,
    action: str,
    resource: str,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert an audit event. Never raises — best-effort logging."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (user_id, action, resource, metadata)
                VALUES ($1, $2, $3, $4)
                """,
                user_id,
                action,
                resource,
                metadata or {},
            )
    except Exception as exc:
        logger.warning("audit_log insert failed: %r", exc, exc_info=True)
