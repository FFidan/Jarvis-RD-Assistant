"""Durable Platform erasure-state persistence."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import asyncpg

logger = logging.getLogger(__name__)


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
    request_id = await conn.fetchval(
        "SELECT platform.request_erasure_v1($1)",
        user_id,
    )
    if request_id is None:
        raise LookupError(f"user {user_id} is not disabled, so erasure cannot be requested")
    return uuid.UUID(str(request_id))


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
        try:
            row = await conn.fetchrow(
                "SELECT * FROM platform.begin_erasure_destructive_v1($1)",
                request_id,
            )
        except asyncpg.RaiseError as exc:
            message = str(exc)
            if "does not exist" in message:
                raise LookupError(f"erasure request {request_id} does not exist") from exc
            if "restore grace" in message:
                raise ValueError("account erasure is still inside the restore grace") from exc
            # Anything else is the capability refusing for a reason this caller
            # does not model — a denied role, say. Record what it said before
            # narrowing it, or the operator sees only the fallback wording.
            logger.warning("erasure phase change was refused: %s", message)
            raise RuntimeError("erasure account is no longer disabled") from exc
        return ErasureRequest(
            request_id=request_id,
            user_id=int(row["user_id"]),
            state=ErasureState(str(row["state"])),
            attempts=int(row["attempts"]),
            resume_state=ErasureState(str(row["resume_state"])),
            eligible_at=row["eligible_at"],
            deleted_at=row["deleted_at"],
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
        "SELECT platform.record_erasure_ack_v1($1, $2, $3)",
        request_id,
        domain,
        receipt,
    )


async def transition(
    pool: asyncpg.Pool,
    request_id: uuid.UUID,
    target: ErasureState,
) -> ErasureState:
    """Apply one exact transition, which the capability validates.

    The state graph itself lives in ``platform.transition_erasure_v1``, so a
    skipped or reversed phase is refused by the database rather than by this
    caller. What is decided here is narrower: a waiting request resumes only
    the phase it recorded, which the graph alone cannot express.

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
    if current in {ErasureState.RETRY_WAIT, ErasureState.ATTENTION_REQUIRED}:
        resume_state = ErasureState(str(row["resume_state"]))
        if target is not resume_state and target is not ErasureState.ATTENTION_REQUIRED:
            raise ValueError(
                f"invalid erasure resume {current.value} -> {target.value}; "
                f"expected {resume_state.value}"
            )
    try:
        persisted = await pool.fetchval(
            "SELECT platform.transition_erasure_v1($1, $2, $3)",
            request_id,
            target.value,
            current.value,
        )
    except asyncpg.RaiseError as exc:
        if "state graph" not in str(exc):
            raise
        raise ValueError(f"invalid erasure transition {current.value} -> {target.value}") from exc
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
    state = await pool.fetchval(
        "SELECT platform.record_erasure_retry_v1($1, $2)",
        request_id,
        resume_state.value,
    )
    if state is None:
        raise RuntimeError("erasure retry phase changed or exhausted")
    return ErasureState(str(state))


async def list_requests(pool: asyncpg.Pool, *, limit: int = 50) -> list[asyncpg.Record]:
    """Return the most recent erasure requests for operator review."""
    return await pool.fetch(
        """SELECT request_id, user_id, state, attempts, resume_state,
                  last_error, next_attempt_at, requested_at
           FROM erasure_requests
           ORDER BY requested_at DESC
           LIMIT $1""",
        limit,
    )


async def resume(pool: asyncpg.Pool, request_id: uuid.UUID) -> ErasureState:
    """Return one stranded request to its recorded phase with a fresh budget.

    Parameters
    ----------
    pool : asyncpg.Pool
        Platform runtime pool.
    request_id : uuid.UUID
        Durable erasure request identifier.

    Returns
    -------
    ErasureState
        The phase the request resumed into.

    Raises
    ------
    LookupError
        If the request does not exist.
    ValueError
        If the request is not stranded, or a newer request supersedes it.
    RuntimeError
        If the capability refused, or the request changed concurrently.
    """
    try:
        state = await pool.fetchval("SELECT platform.resume_erasure_v1($1)", request_id)
    except asyncpg.RaiseError as exc:
        message = str(exc)
        if "does not exist" in message:
            raise LookupError(f"erasure request {request_id} does not exist") from exc
        if "is not allowed" in message:
            # A denied role, say: record what the capability said before
            # narrowing it, or the operator sees only the fallback wording.
            logger.warning("erasure resume was refused: %s", message)
            raise RuntimeError("erasure resume was refused") from exc
        raise ValueError(message) from exc
    if state is None:
        raise RuntimeError("erasure request changed during resume")
    return ErasureState(str(state))


__all__ = [
    "ErasureRequest",
    "ErasureState",
    "acknowledge",
    "begin_destructive_phases",
    "create_or_get_request",
    "acknowledged_domains",
    "due_request_ids",
    "get_request",
    "list_requests",
    "record_retry",
    "resume",
    "transition",
]
