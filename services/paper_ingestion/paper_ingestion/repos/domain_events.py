"""Small transactional Research outbox for Learning projections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

import asyncpg
import httpx
from jarvis_common.service_auth import (
    ServiceCommand,
    ServiceCommandUnavailableError,
    authorize_service_command,
)
from jarvis_common.telemetry import event_context, restored_correlation, restored_span
from opentelemetry.trace import SpanKind

EventType = Literal["paper.read", "paper.deleted"]


@dataclass(frozen=True, slots=True)
class DomainDeliverySettings:
    """Endpoints and credential needed to deliver Research projections."""

    platform_url: str
    learning_url: str
    service_token: str


_ACKNOWLEDGED_PRIVATE_PAPER_DELETES = (
    "DELETE FROM author_alert_log WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_contradictions WHERE (paper_a_id = $1 OR paper_b_id = $1) AND user_id = $2",
    """WITH deleted AS (
           DELETE FROM paper_entities
           WHERE paper_id = $1 AND user_id = $2
           RETURNING entity_id
       )
       UPDATE entities AS entity
       SET paper_count = (
           SELECT count(*) FROM paper_entities AS remaining
           WHERE remaining.entity_id = entity.id
       )
       WHERE entity.id IN (SELECT entity_id FROM deleted)""",
    "DELETE FROM paper_extractions WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_highlights WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_notes WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_recommendations WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_summaries WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_user_zotero_links WHERE paper_id = $1 AND user_id = $2",
    """WITH deleted AS (
           DELETE FROM pulse_cards
           WHERE paper_id = $1 AND user_id = $2
           RETURNING deck_id
       )
       UPDATE pulse_decks AS deck
       SET card_count = (
           SELECT count(*) FROM pulse_cards AS remaining
           WHERE remaining.deck_id = deck.id
       )
       WHERE deck.id IN (SELECT deck_id FROM deleted)""",
    "DELETE FROM recommendation_feedback WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_user_state WHERE paper_id = $1 AND user_id = $2",
)


async def record_event(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    *,
    event_type: EventType,
    user_id: int,
    paper_id: int,
) -> uuid.UUID:
    """Persist one immutable transition event inside the caller transaction."""
    event_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO domain_events (id, event_type, user_id, paper_id, payload)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (event_type, user_id, paper_id)
            WHERE event_type = 'paper.deleted'
              AND delivered_at IS NULL AND dead_lettered_at IS NULL
        DO NOTHING
        """,
        event_id,
        event_type,
        user_id,
        paper_id,
        event_context(),
    )
    row = await conn.fetchval(
        """SELECT id FROM domain_events
           WHERE event_type = $1 AND user_id = $2 AND paper_id = $3
           ORDER BY created_at DESC LIMIT 1""",
        event_type,
        user_id,
        paper_id,
    )
    return uuid.UUID(str(row))


async def deliver_pending_events(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    *,
    settings: DomainDeliverySettings,
    limit: int = 20,
) -> int:
    """Deliver pending events without retaining a database connection over HTTP."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, event_type, user_id, paper_id, payload
            FROM domain_events
            WHERE delivered_at IS NULL AND dead_lettered_at IS NULL AND next_attempt_at <= NOW()
            ORDER BY created_at
            LIMIT $1
            """,
            limit,
        )
    delivered = 0
    for row in rows:
        event_id = uuid.UUID(str(row["id"]))
        event_type = str(row["event_type"])
        path = (
            "/internal/domains/paper-read"
            if event_type == "paper.read"
            else "/internal/domains/paper-deleted"
        )
        payload = row.get("payload") if hasattr(row, "get") else None
        payload = payload if isinstance(payload, dict) else {}
        try:
            with restored_correlation(payload.get("correlation_id")):
                with restored_span(
                    carrier=payload,
                    service="research",
                    name="outbox.delivery",
                    kind=SpanKind.PRODUCER,
                ):
                    headers = await authorize_service_command(
                        client,
                        platform_url=settings.platform_url,
                        principal="research",
                        token=settings.service_token,
                        command=ServiceCommand(
                            audience="learning",
                            method="POST",
                            path=path,
                            user_id=int(row["user_id"]),
                            request_id=str(event_id),
                        ),
                    )
                    response = await client.post(
                        f"{settings.learning_url.rstrip('/')}{path}",
                        headers=headers,
                        json={
                            "request_id": str(event_id),
                            "user_id": int(row["user_id"]),
                            "paper_id": int(row["paper_id"]),
                        },
                        timeout=10.0,
                    )
                    response.raise_for_status()
                    acknowledgement = response.json()
                    if (
                        not isinstance(acknowledgement, dict)
                        or acknowledgement.get("acknowledged") is not True
                    ):
                        raise ServiceCommandUnavailableError(
                            "Learning acknowledgement is unavailable"
                        )
        except (httpx.HTTPError, ServiceCommandUnavailableError, ValueError):
            await _mark_failure(pool, event_id)
        else:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """UPDATE domain_events
                           SET delivered_at = NOW(), last_error = NULL
                           WHERE id = $1 AND delivered_at IS NULL""",
                        event_id,
                    )
                    if event_type == "paper.deleted":
                        await _purge_acknowledged_deletion(conn, event_id)
            delivered += 1
    return delivered


async def _mark_failure(pool: asyncpg.Pool, event_id: uuid.UUID) -> None:
    """Persist bounded retry state without leaking downstream error details."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE domain_events
            SET attempts = attempts + 1,
                last_error = 'learning command unavailable',
                next_attempt_at = NOW() + LEAST(
                    INTERVAL '1 hour', (POWER(2, attempts) * INTERVAL '30 seconds')
                ),
                dead_lettered_at = CASE WHEN attempts + 1 >= 8 THEN NOW() END
            WHERE id = $1 AND delivered_at IS NULL AND dead_lettered_at IS NULL
            """,
            event_id,
        )


async def _purge_acknowledged_deletion(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, event_id: uuid.UUID
) -> None:
    """Remove retained Research-private rows only after Learning acknowledged."""
    deletion = await conn.fetchrow(
        """DELETE FROM pending_paper_deletions WHERE event_id = $1
           RETURNING user_id, paper_id""",
        event_id,
    )
    if deletion is None:
        return
    user_id, paper_id = int(deletion["user_id"]), int(deletion["paper_id"])
    for statement in _ACKNOWLEDGED_PRIVATE_PAPER_DELETES:
        await conn.execute(statement, paper_id, user_id)


__all__ = ["DomainDeliverySettings", "deliver_pending_events", "record_event"]
