"""Per-paper advisory locking shared by every PDF workflow mutation path.

The session-level advisory lock and the pooled try-lock loop that serialize
Qdrant writes with their matching PostgreSQL metadata commits.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import asyncpg

from paper_ingestion.db_types import ConnLike
from paper_ingestion.services.pdf_errors import PDFUserFacingError

_PAPER_LOCK_RETRY_INITIAL_SECONDS = 0.05
_PAPER_LOCK_RETRY_MAX_SECONDS = 1.0
# Total time a caller waits for a contended per-paper lock before giving up.
_PAPER_LOCK_MAX_WAIT_SECONDS = 600


def paper_locked_error(paper_id: int) -> PDFUserFacingError:
    """Build the refusal both per-paper lock waits raise once they give up.

    Shared so the try-lock probe loop and the blocking summarize lock cannot
    drift into telling the same person two different things.
    """
    return PDFUserFacingError(
        f"Paper {paper_id} is locked by another long-running operation; retry after it finishes."
    )


@asynccontextmanager
async def advisory_lock(
    conn: ConnLike, lock_key: int, paper_id: int, timeout_s: float | None = None
):
    """Acquire a PostgreSQL session-level advisory lock and release on exit.

    Parameters
    ----------
    conn : ConnLike
        Active asyncpg connection or pool proxy.
    lock_key : int
        First key component (classifies the lock type, e.g. 1=process, 2=summarize).
    paper_id : int
        Second key component (paper DB ID); combined with *lock_key* forms the
        unique 64-bit advisory lock identifier.
    timeout_s : float | None
        Bound on the wait for a contended lock. ``None`` waits indefinitely, as
        the reconciliation paths do while holding their own connection.

    Notes
    -----
    Uses ``pg_advisory_lock`` (blocking) rather than ``pg_try_advisory_lock``.
    The paper-processing and reconciliation paths intentionally keep this
    per-paper lock across Qdrant I/O so deterministic point replacement and
    PostgreSQL metadata publication form one serialized generation. Different
    papers use different lock keys and continue concurrently.

    ``lock_timeout`` is a session setting and this lock is taken outside a
    transaction, so ``SET LOCAL`` would be a no-op; the outer ``finally`` resets
    it explicitly, including on the timeout path where the lock was never
    acquired. asyncpg's pool also resets a connection on release, but that only
    covers a task that died without unwinding.
    """
    if timeout_s is not None:
        # SET takes no bind parameters; the int cast is what keeps this literal safe.
        await conn.execute(f"SET lock_timeout = '{int(timeout_s)}s'")
    try:
        await conn.execute("SELECT pg_advisory_lock($1, $2)", lock_key, paper_id)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1, $2)", lock_key, paper_id)
    finally:
        if timeout_s is not None:
            await conn.execute("SET lock_timeout = DEFAULT")


@asynccontextmanager
async def _paper_mutation_connection(db_pool: asyncpg.Pool, paper_id: int):
    """Yield a pooled connection holding the shared per-paper mutation lock.

    A contended probe returns its connection to the pool before sleeping, so
    duplicate requests for one long-running PDF cannot consume every pool slot.
    Once acquired, the same connection and session-level lock span the complete
    Qdrant plus PostgreSQL publication.

    ``pg_try_advisory_lock`` never waits, so ``lock_timeout`` cannot bound this
    loop; the accumulated sleep time is the deadline instead.
    """
    retry_delay = _PAPER_LOCK_RETRY_INITIAL_SECONDS
    waited = 0.0
    while True:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT pg_try_advisory_lock($1, $2) AS acquired",
                1,
                paper_id,
            )
            acquired = row is not None and bool(row["acquired"])
            if acquired:
                try:
                    yield conn
                finally:
                    unlock_task = asyncio.create_task(
                        conn.execute("SELECT pg_advisory_unlock($1, $2)", 1, paper_id)
                    )
                    try:
                        await asyncio.shield(unlock_task)
                    except asyncio.CancelledError:
                        await unlock_task
                        raise
                return
        if waited >= _PAPER_LOCK_MAX_WAIT_SECONDS:
            raise paper_locked_error(paper_id)
        await asyncio.sleep(retry_delay)
        waited += retry_delay
        retry_delay = min(retry_delay * 2, _PAPER_LOCK_RETRY_MAX_SECONDS)
