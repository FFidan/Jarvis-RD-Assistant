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

# Delivery is retried with exponential backoff before an event is dead-lettered.
# The series below — 30 seconds, doubling, capped at six hours — spans about
# fourteen hours across the attempts that precede the last one, so an ordinary
# maintenance window cannot exhaust an event's retries and turn a planned
# outage into permanently retained rows.
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_CEILING = "6 hours"
MAX_DELIVERY_ATTEMPTS = 12


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
    user_id: int | None = None,
) -> int:
    """Deliver pending events without retaining a database connection over HTTP.

    Each event costs an authorization call plus a delivery call, so a caller
    that runs inside a request handler passes ``user_id`` (and a small ``limit``)
    to stay within its own owner's work. The scheduler leaves both at their
    defaults and drains the whole outbox.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, event_type, user_id, paper_id, payload
            FROM domain_events
            WHERE delivered_at IS NULL AND dead_lettered_at IS NULL AND next_attempt_at <= NOW()
              AND ($2::bigint IS NULL OR user_id = $2)
            ORDER BY created_at
            LIMIT $1
            """,
            limit,
            user_id,
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
            f"""
            UPDATE domain_events
            SET attempts = attempts + 1,
                last_error = 'learning command unavailable',
                next_attempt_at = NOW() + LEAST(
                    INTERVAL '{RETRY_BACKOFF_CEILING}',
                    (POWER(2, attempts) * INTERVAL '{RETRY_BACKOFF_BASE_SECONDS} seconds')
                ),
                dead_lettered_at = CASE
                    WHEN attempts + 1 >= {MAX_DELIVERY_ATTEMPTS} THEN NOW()
                END
            WHERE id = $1 AND delivered_at IS NULL AND dead_lettered_at IS NULL
            """,
            event_id,
        )


async def requeue_dead_lettered_events(pool: asyncpg.Pool, *, user_id: int | None = None) -> int:
    """Return dead-lettered events to the delivery queue and report how many moved.

    Dead-lettering is otherwise terminal, and an undelivered ``paper.deleted``
    keeps a deleted paper's Research-private rows retained, so an operator needs
    a way to replay events once the cause of the outage is fixed. ``user_id``
    narrows the replay to one owner.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE domain_events AS stalled
            SET dead_lettered_at = NULL,
                attempts = 0,
                next_attempt_at = NOW(),
                last_error = NULL
            WHERE stalled.dead_lettered_at IS NOT NULL
              AND stalled.delivered_at IS NULL
              AND ($1::bigint IS NULL OR stalled.user_id = $1)
              -- Undelivered deletions are unique per owner and paper. Requeueing
              -- one that has any undelivered sibling would break that
              -- uniqueness, so such rows stay put and are resolved by hand.
              AND (
                  stalled.event_type <> 'paper.deleted'
                  OR NOT EXISTS (
                      SELECT 1
                      FROM domain_events AS sibling
                      WHERE sibling.event_type = 'paper.deleted'
                        AND sibling.user_id = stalled.user_id
                        AND sibling.paper_id = stalled.paper_id
                        AND sibling.delivered_at IS NULL
                        AND sibling.id <> stalled.id
                  )
              )
            RETURNING stalled.id
            """,
            user_id,
        )
    return len(rows)


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


__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "RETRY_BACKOFF_BASE_SECONDS",
    "RETRY_BACKOFF_CEILING",
    "DomainDeliverySettings",
    "deliver_pending_events",
    "record_event",
    "requeue_dead_lettered_events",
]
