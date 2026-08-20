"""Platform-owned configuration persistence and durable delivery state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import asyncpg


class DeliveryState(StrEnum):
    """Observable state of one latest-desired Research delivery."""

    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConfigDelivery:
    """One versioned configuration delivery without retained plaintext secrets."""

    delivery_id: uuid.UUID
    scope_user_id: int
    user_id: int | None
    user_role: str | None
    session_id: str | None
    zotero_scope_changed: bool
    key: str
    value: Any
    encrypted_value: bytes | None
    attempts: int
    next_attempt_at: datetime


@dataclass(frozen=True, slots=True)
class ConfigWrite:
    """Platform-owned value and actor context persisted atomically."""

    user_id: int | None
    actor_user_id: int
    user_role: str | None
    session_id: str | None
    key: str
    value: Any
    encrypted_value: bytes | None


async def persist_value(
    pool: asyncpg.Pool,
    write: ConfigWrite,
) -> uuid.UUID:
    """Atomically persist a Platform value and its latest delivery version."""
    delivery_id = uuid.uuid4()
    scope_user_id = write.user_id or 0
    async with pool.acquire() as conn, conn.transaction():
        zotero_scope_changed = False
        if write.user_id is not None and write.key in {
            "zotero.library_type",
            "zotero.user_id",
            "zotero.group_id",
        }:
            previous = await conn.fetchrow(
                """SELECT value FROM user_config
                   WHERE user_id = $1 AND key = $2 FOR UPDATE""",
                write.user_id,
                write.key,
            )
            zotero_scope_changed = previous is None or previous["value"] != write.value
        if write.encrypted_value is not None:
            await conn.execute(
                """INSERT INTO user_config (user_id, key, value, encrypted_value)
                   VALUES ($1, $2, NULL, $3)
                   ON CONFLICT (user_id, key) DO UPDATE
                       SET value = NULL, encrypted_value = EXCLUDED.encrypted_value,
                           updated_at = NOW()""",
                write.user_id,
                write.key,
                write.encrypted_value,
            )
        else:
            await conn.execute(
                """INSERT INTO user_config (user_id, key, value, encrypted_value)
                   VALUES ($1, $2, $3::jsonb, NULL)
                   ON CONFLICT (user_id, key) DO UPDATE
                       SET value = EXCLUDED.value, encrypted_value = NULL,
                           updated_at = NOW()""",
                write.user_id,
                write.key,
                write.value,
            )
        await conn.execute(
            """INSERT INTO config_deliveries
               (scope_user_id, actor_user_id, key, delivery_id, user_role, session_id,
                zotero_scope_changed, state, attempts, next_attempt_at, last_error, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', 0, NOW(), NULL, NOW())
               ON CONFLICT (scope_user_id, key) DO UPDATE
                   SET delivery_id = EXCLUDED.delivery_id,
                       actor_user_id = EXCLUDED.actor_user_id,
                       user_role = EXCLUDED.user_role,
                       session_id = EXCLUDED.session_id,
                       zotero_scope_changed = EXCLUDED.zotero_scope_changed,
                       state = 'pending', attempts = 0,
                       next_attempt_at = NOW(), last_error = NULL,
                       updated_at = NOW()""",
            scope_user_id,
            write.actor_user_id,
            write.key,
            delivery_id,
            write.user_role,
            write.session_id,
            zotero_scope_changed,
        )
    return delivery_id


async def get_delivery(pool: asyncpg.Pool, delivery_id: uuid.UUID) -> ConfigDelivery | None:
    """Return the current desired delivery when its version still matches."""
    row = await pool.fetchrow(
        """SELECT delivery.delivery_id, delivery.scope_user_id,
                  delivery.actor_user_id AS user_id,
                  delivery.user_role, delivery.session_id, delivery.zotero_scope_changed,
                  delivery.key, config.value,
                  config.encrypted_value, delivery.attempts,
                  delivery.next_attempt_at
           FROM config_deliveries AS delivery
           JOIN user_config AS config
             ON config.user_id IS NOT DISTINCT FROM NULLIF(delivery.scope_user_id, 0)
            AND config.key = delivery.key
           WHERE delivery.delivery_id = $1 AND delivery.state = 'pending'""",
        delivery_id,
    )
    if row is None:
        return None
    return ConfigDelivery(
        delivery_id=uuid.UUID(str(row["delivery_id"])),
        scope_user_id=int(row["scope_user_id"]),
        user_id=int(row["user_id"]) if row["user_id"] is not None else None,
        user_role=row["user_role"],
        session_id=row["session_id"],
        zotero_scope_changed=bool(row["zotero_scope_changed"]),
        key=str(row["key"]),
        value=row["value"],
        encrypted_value=row["encrypted_value"],
        attempts=int(row["attempts"]),
        next_attempt_at=row["next_attempt_at"],
    )


async def due_delivery_ids(pool: asyncpg.Pool, *, limit: int = 20) -> list[uuid.UUID]:
    """Return bounded pending delivery versions eligible for retry."""
    rows = await pool.fetch(
        """SELECT delivery_id FROM config_deliveries
           WHERE state = 'pending' AND next_attempt_at <= NOW()
           ORDER BY next_attempt_at, updated_at LIMIT $1""",
        limit,
    )
    return [uuid.UUID(str(row["delivery_id"])) for row in rows]


async def mark_applied(pool: asyncpg.Pool, delivery_id: uuid.UUID) -> bool:
    """Acknowledge only the same latest version that Research applied."""
    status = await pool.execute(
        """UPDATE config_deliveries
           SET state = 'applied', last_error = NULL, updated_at = NOW()
           WHERE delivery_id = $1 AND state = 'pending'""",
        delivery_id,
    )
    return status == "UPDATE 1"


def _validate_research_config_effects(
    roles: list[str],
    pending: bool | None,
    effective_num_ctx_role: str | None,
    effective_num_ctx_value: int | None,
) -> set[str]:
    """Validate a typed Research effect report and return its role set."""
    allowed_roles = {"smart", "fast", "embed"}
    role_set = set(roles)
    if not role_set <= allowed_roles:
        raise ValueError("Research returned an unknown LiteLLM role")
    if pending is None and role_set:
        raise ValueError("Research omitted the LiteLLM delivery state")
    if pending is not None and not role_set:
        raise ValueError("Research returned a LiteLLM state without roles")
    if (effective_num_ctx_role is None) != (effective_num_ctx_value is None):
        raise ValueError("Research returned an incomplete effective context update")
    if effective_num_ctx_role is not None and effective_num_ctx_role not in allowed_roles:
        raise ValueError("Research returned an unknown effective context role")
    if effective_num_ctx_value is not None and effective_num_ctx_value <= 0:
        raise ValueError("Research returned an invalid effective context value")
    return role_set


async def apply_research_config_effects(
    pool: asyncpg.Pool,
    *,
    roles: list[str],
    pending: bool | None,
    effective_num_ctx_role: str | None,
    effective_num_ctx_value: int | None,
) -> None:
    """Persist typed Platform-owned state reported by Research delivery."""
    role_set = _validate_research_config_effects(
        roles,
        pending,
        effective_num_ctx_role,
        effective_num_ctx_value,
    )

    async with pool.acquire() as conn, conn.transaction():
        if pending is not None:
            row = await conn.fetchrow(
                """SELECT value FROM user_config
                   WHERE key = 'llm.delivery_pending' AND user_id IS NULL
                   FOR UPDATE"""
            )
            raw = row["value"] if row is not None else None
            current = {str(item) for item in raw} if isinstance(raw, list) else set()
            updated = current | role_set if pending else current - role_set
            if updated != current or not isinstance(raw, list):
                await conn.execute(
                    """INSERT INTO user_config (user_id, key, value)
                       VALUES (NULL, 'llm.delivery_pending', $1::jsonb)
                       ON CONFLICT (user_id, key) DO UPDATE
                           SET value = EXCLUDED.value, updated_at = NOW()""",
                    sorted(updated),
                )
        if effective_num_ctx_role is not None:
            await conn.execute(
                """INSERT INTO user_config (user_id, key, value)
                   VALUES (NULL, $1, $2::jsonb)
                   ON CONFLICT (user_id, key) DO UPDATE
                       SET value = EXCLUDED.value, updated_at = NOW()""",
                f"llm.{effective_num_ctx_role}_num_ctx",
                effective_num_ctx_value,
            )


async def record_retry(pool: asyncpg.Pool, delivery_id: uuid.UUID, error: str) -> bool:
    """Schedule a bounded exponential retry for the same latest version."""
    status = await pool.execute(
        """UPDATE config_deliveries
           SET state = CASE WHEN attempts + 1 >= 8 THEN 'failed' ELSE 'pending' END,
               attempts = LEAST(attempts + 1, 8),
               next_attempt_at = NOW() + make_interval(
                   secs => LEAST(300, (2 ^ LEAST(attempts + 1, 8))::integer)
               ),
               last_error = LEFT($2, 500), updated_at = NOW()
           WHERE delivery_id = $1 AND state = 'pending'""",
        delivery_id,
        error,
    )
    return status == "UPDATE 1"


async def delivery_state(pool: asyncpg.Pool, *, user_id: int | None, key: str) -> DeliveryState:
    """Return visible delivery state, defaulting legacy rows to applied."""
    async with pool.acquire() as conn:
        state = await conn.fetchval(
            """SELECT state FROM config_deliveries
               WHERE scope_user_id = $1 AND key = $2""",
            user_id or 0,
            key,
        )
    return DeliveryState(str(state)) if state is not None else DeliveryState.APPLIED


__all__ = [
    "ConfigDelivery",
    "ConfigWrite",
    "DeliveryState",
    "apply_research_config_effects",
    "delivery_state",
    "due_delivery_ids",
    "get_delivery",
    "mark_applied",
    "persist_value",
    "record_retry",
]
