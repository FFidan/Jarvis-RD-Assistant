"""Postgres session-level advisory lock context manager.

Session-level locks are required because Pulse runs are minutes long —
transaction-level locks would auto-release on commit/rollback, which is
too short-lived for a multi-step pipeline.

Usage::

    async with AdvisoryLock(pool, key1=_kind_lock_key("arxiv")) as acquired:
        if not acquired:
            return  # another process holds the lock; skip this run
        # … do the long-running work …
"""

from __future__ import annotations

import hashlib
from typing import Any

import asyncpg


class AdvisoryLock:
    """Session-level Postgres advisory lock.

    Uses a dedicated connection (required so the lock lives beyond any single
    transaction boundary).  The connection is released on ``__aexit__``.

    Parameters
    ----------
    pool:
        asyncpg connection pool.
    key1:
        First 32-bit lock key.
    key2:
        Second 32-bit lock key (default ``0``).
    """

    def __init__(self, pool: asyncpg.Pool, key1: int, key2: int = 0) -> None:
        self._pool = pool
        self._key1 = key1
        self._key2 = key2
        self._conn: Any | None = None
        self._locked = False

    async def __aenter__(self) -> bool:
        """Try to acquire the advisory lock.

        Returns
        -------
        bool
            ``True`` if the lock was acquired by this call; ``False`` if it is
            already held by another session (non-blocking ``pg_try_advisory_lock``).

        Notes
        -----
        Acquires a **dedicated** connection from the pool.  If the lock is not
        obtained the connection is released immediately so it is not held idle.
        On exception the connection is always released.
        """
        self._conn = await self._pool.acquire()
        try:
            conn = self._conn
            if conn is None:
                raise RuntimeError("advisory lock connection was not acquired")
            row = await conn.fetchrow(
                "SELECT pg_try_advisory_lock($1, $2) AS got",
                self._key1,
                self._key2,
            )
            self._locked = bool(row["got"]) if row is not None else False
            if not self._locked:
                # Lock not obtained — release the connection straight away.
                await self._pool.release(self._conn)
                self._conn = None
            return self._locked
        except Exception:
            if self._conn is not None:
                await self._pool.release(self._conn)
                self._conn = None
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Release the advisory lock and return the connection to the pool.

        Safe to call even if the lock was never acquired (no-op).
        """
        if self._conn is None or not self._locked:
            return
        try:
            await self._conn.execute(
                "SELECT pg_advisory_unlock($1, $2)",
                self._key1,
                self._key2,
            )
        finally:
            await self._pool.release(self._conn)
            self._conn = None
            self._locked = False


def _kind_lock_key(kind: str) -> int:
    """Return a deterministic 32-bit positive integer from a kind string.

    Used to map source-type / kind strings to ``pg_advisory_lock`` key1 values.

    Parameters
    ----------
    kind:
        Arbitrary string identifier (e.g. ``"arxiv"``).

    Returns
    -------
    int
        A value in ``[0, 2**31 - 1]``.
    """
    return int.from_bytes(hashlib.sha256(kind.encode()).digest()[:4], "big") & 0x7FFF_FFFF
