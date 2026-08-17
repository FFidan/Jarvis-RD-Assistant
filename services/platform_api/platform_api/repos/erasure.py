"""Durable Platform erasure-state persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import asyncpg


class ErasureState(StrEnum):
    """Persisted states in the account-erasure transition graph."""

    REQUESTED = "requested"
    QDRANT_PENDING = "qdrant_pending"
    RESEARCH_PENDING = "research_pending"
    LEARNING_PENDING = "learning_pending"
    READY = "ready"
    EXECUTING = "executing"
    COMPLETE = "complete"
    RETRY_WAIT = "retry_wait"
    ATTENTION_REQUIRED = "attention_required"


_RESUMABLE_STATES = frozenset(
    {
        ErasureState.QDRANT_PENDING,
        ErasureState.RESEARCH_PENDING,
        ErasureState.LEARNING_PENDING,
        ErasureState.EXECUTING,
    }
)
_TRANSITIONS: dict[ErasureState, frozenset[ErasureState]] = {
    ErasureState.REQUESTED: frozenset({ErasureState.QDRANT_PENDING}),
    ErasureState.QDRANT_PENDING: frozenset(
        {
            ErasureState.RESEARCH_PENDING,
            ErasureState.RETRY_WAIT,
            ErasureState.ATTENTION_REQUIRED,
        }
    ),
    ErasureState.RESEARCH_PENDING: frozenset(
        {
            ErasureState.LEARNING_PENDING,
            ErasureState.RETRY_WAIT,
            ErasureState.ATTENTION_REQUIRED,
        }
    ),
    ErasureState.LEARNING_PENDING: frozenset(
        {
            ErasureState.READY,
            ErasureState.RETRY_WAIT,
            ErasureState.ATTENTION_REQUIRED,
        }
    ),
    ErasureState.READY: frozenset({ErasureState.EXECUTING}),
    ErasureState.EXECUTING: frozenset(
        {
            ErasureState.COMPLETE,
            ErasureState.RETRY_WAIT,
            ErasureState.ATTENTION_REQUIRED,
        }
    ),
    ErasureState.RETRY_WAIT: _RESUMABLE_STATES | {ErasureState.ATTENTION_REQUIRED},
    ErasureState.ATTENTION_REQUIRED: _RESUMABLE_STATES,
    ErasureState.COMPLETE: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ErasureRequest:
    """One durable Platform erasure request."""

    request_id: uuid.UUID
    user_id: int
    state: ErasureState
    attempts: int
    resume_state: ErasureState
    eligible_at: datetime
    deleted_at: datetime | None


async def create_or_get_request(conn: asyncpg.Connection, user_id: int) -> uuid.UUID:
    """Create one active request or return its stable idempotency key."""
    active = await conn.fetchval(
        """SELECT request_id FROM erasure_requests
           WHERE user_id = $1 AND state NOT IN ('complete', 'attention_required')
           ORDER BY requested_at DESC LIMIT 1""",
        user_id,
    )
    if active is not None:
        return uuid.UUID(str(active))
    request_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO erasure_requests
           (request_id, user_id, state, resume_state, eligible_at)
           SELECT $1, id, 'requested', 'qdrant_pending',
                  deleted_at + INTERVAL '30 days'
           FROM users
           WHERE id = $2 AND deleted_at IS NOT NULL""",
        request_id,
        user_id,
    )
    return request_id


async def get_request(pool: asyncpg.Pool, request_id: uuid.UUID) -> ErasureRequest | None:
    """Return a request snapshot without retaining the connection for I/O."""
    row = await pool.fetchrow(
        """SELECT request_id, er.user_id, state, attempts, resume_state,
                  eligible_at, users.deleted_at
           FROM erasure_requests AS er
           LEFT JOIN users ON users.id = er.user_id
           WHERE request_id = $1""",
        request_id,
    )
    if row is None:
        return None
    return ErasureRequest(
        request_id=uuid.UUID(str(row["request_id"])),
        user_id=int(row["user_id"]),
        state=ErasureState(str(row["state"])),
        attempts=int(row["attempts"]),
        resume_state=ErasureState(str(row["resume_state"])),
        eligible_at=row["eligible_at"],
        deleted_at=row["deleted_at"],
    )


async def begin_destructive_phases(pool: asyncpg.Pool, request_id: uuid.UUID) -> ErasureRequest:
    """Serialize the grace-boundary transition with account restoration."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """SELECT request_id, user_id, state, attempts, resume_state, eligible_at
               FROM erasure_requests WHERE request_id = $1 FOR UPDATE""",
            request_id,
        )
        if row is None:
            raise LookupError(f"erasure request {request_id} does not exist")
        user = await conn.fetchrow(
            "SELECT deleted_at FROM users WHERE id = $1 FOR UPDATE",
            row["user_id"],
        )
        if user is None or user["deleted_at"] is None:
            raise RuntimeError("erasure account is no longer disabled")
        state = ErasureState(str(row["state"]))
        if state is ErasureState.REQUESTED:
            eligible = await conn.fetchval(
                """SELECT $1 <= NOW() AND $2::timestamptz + INTERVAL '30 days' <= NOW()""",
                row["eligible_at"],
                user["deleted_at"],
            )
            if eligible is not True:
                raise ValueError("account erasure is still inside the restore grace")
            await conn.execute(
                """UPDATE erasure_requests SET state = 'qdrant_pending'
                   WHERE request_id = $1 AND state = 'requested'""",
                request_id,
            )
            state = ErasureState.QDRANT_PENDING
        return ErasureRequest(
            request_id=uuid.UUID(str(row["request_id"])),
            user_id=int(row["user_id"]),
            state=state,
            attempts=int(row["attempts"]),
            resume_state=ErasureState(str(row["resume_state"])),
            eligible_at=row["eligible_at"],
            deleted_at=user["deleted_at"],
        )


async def due_request_ids(pool: asyncpg.Pool, *, limit: int = 20) -> list[uuid.UUID]:
    """Return incomplete requests whose account-restore grace has elapsed."""
    rows = await pool.fetch(
        """SELECT er.request_id
           FROM erasure_requests AS er
           JOIN users ON users.id = er.user_id
           WHERE er.state IN (
               'requested', 'qdrant_pending', 'research_pending',
               'learning_pending', 'retry_wait'
           )
             AND er.next_attempt_at <= NOW()
             AND er.eligible_at <= NOW()
             AND users.deleted_at IS NOT NULL
             AND users.deleted_at + INTERVAL '30 days' <= NOW()
           ORDER BY er.next_attempt_at, er.requested_at
           LIMIT $1""",
        limit,
    )
    return [uuid.UUID(str(row["request_id"])) for row in rows]


async def acknowledge(
    pool: asyncpg.Pool, request_id: uuid.UUID, domain: str, receipt: dict[str, object]
) -> None:
    """Persist an idempotent domain acknowledgement."""
    await pool.execute(
        """
        INSERT INTO erasure_acknowledgements (request_id, domain, receipt)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT (request_id, domain)
        DO UPDATE SET receipt = EXCLUDED.receipt, acknowledged_at = NOW()
        """,
        request_id,
        domain,
        receipt,
    )


async def transition(
    pool: asyncpg.Pool,
    request_id: uuid.UUID,
    target: ErasureState,
) -> ErasureState:
    """Apply one exact transition from the manifest-defined state graph.

    Parameters
    ----------
    pool : asyncpg.Pool
        Platform runtime pool.
    request_id : uuid.UUID
        Durable erasure request identifier.
    target : ErasureState
        Desired next state.

    Returns
    -------
    ErasureState
        The persisted target state.

    Raises
    ------
    LookupError
        If the request does not exist.
    ValueError
        If the transition would skip or reverse a durable phase.
    RuntimeError
        If another coordinator changed the request concurrently.
    """
    row = await pool.fetchrow(
        "SELECT state, resume_state FROM erasure_requests WHERE request_id = $1",
        request_id,
    )
    if row is None:
        raise LookupError(f"erasure request {request_id} does not exist")
    current = ErasureState(str(row["state"]))
    if current is target:
        return current
    if target not in _TRANSITIONS[current]:
        raise ValueError(f"invalid erasure transition {current.value} -> {target.value}")
    if current in {ErasureState.RETRY_WAIT, ErasureState.ATTENTION_REQUIRED}:
        resume_state = ErasureState(str(row["resume_state"]))
        if target is not resume_state and target is not ErasureState.ATTENTION_REQUIRED:
            raise ValueError(
                f"invalid erasure resume {current.value} -> {target.value}; "
                f"expected {resume_state.value}"
            )
    persisted = await pool.fetchval(
        """UPDATE erasure_requests
           SET state = $2, last_error = NULL,
               next_attempt_at = CASE WHEN $2 = 'retry_wait' THEN next_attempt_at ELSE NOW() END
           WHERE request_id = $1 AND state = $3
           RETURNING state""",
        request_id,
        target.value,
        current.value,
    )
    if persisted is None:
        raise RuntimeError("erasure request changed during transition")
    return ErasureState(str(persisted))


async def acknowledged_domains(pool: asyncpg.Pool, request_id: uuid.UUID) -> set[str]:
    """Return durable completion facts for resumable coordinator phases."""
    rows = await pool.fetch(
        "SELECT domain FROM erasure_acknowledgements WHERE request_id = $1", request_id
    )
    return {str(row["domain"]) for row in rows}


async def record_retry(
    pool: asyncpg.Pool,
    request_id: uuid.UUID,
    resume_state: ErasureState,
) -> ErasureState:
    """Persist bounded retry state and return the resulting state."""
    if resume_state not in _RESUMABLE_STATES:
        raise ValueError(f"{resume_state.value} is not a resumable erasure phase")
    state = await pool.fetchval(
        """
        UPDATE erasure_requests
        SET attempts = attempts + 1,
            state = CASE WHEN attempts + 1 >= 8 THEN 'attention_required' ELSE 'retry_wait' END,
            resume_state = $2,
            last_error = 'owner command unavailable',
            next_attempt_at = NOW() + LEAST(
                INTERVAL '1 hour', POWER(2, attempts) * INTERVAL '30 seconds'
            )
        WHERE request_id = $1 AND state = $3 AND attempts < 8
        RETURNING state
        """,
        request_id,
        resume_state.value,
        resume_state.value,
    )
    if state is None:
        raise RuntimeError("erasure retry phase changed or exhausted")
    return ErasureState(str(state))


async def cancel_active_request(conn: asyncpg.Connection, user_id: int) -> None:
    """Delete pre-finalization work when an account is restored in grace."""
    await conn.execute(
        "DELETE FROM erasure_requests WHERE user_id = $1 AND state <> 'complete'",
        user_id,
    )


__all__ = [
    "ErasureRequest",
    "ErasureState",
    "acknowledge",
    "begin_destructive_phases",
    "cancel_active_request",
    "create_or_get_request",
    "acknowledged_domains",
    "due_request_ids",
    "get_request",
    "record_retry",
    "transition",
]
