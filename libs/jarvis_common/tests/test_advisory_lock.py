"""Cancellation-safety tests for jarvis_common.advisory_lock.AdvisoryLock.

``asyncio.CancelledError`` is a ``BaseException``, so ``__aenter__``'s narrow
``except Exception`` cleanup leaked the checked-out connection on a cancel during
the lock query; it now catches ``BaseException``. ``__aexit__`` already released
via ``try/finally`` — its test guards that that stays true.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.advisory_lock import AdvisoryLock


def _mock_pool(conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_aenter_cancelled_during_lock_query_releases_conn() -> None:
    """Cancel during pg_try_advisory_lock must release the conn back to the pool."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=asyncio.CancelledError())
    pool = _mock_pool(conn)

    lock = AdvisoryLock(pool, key1=1)
    with pytest.raises(asyncio.CancelledError):
        await lock.__aenter__()

    pool.release.assert_awaited_once_with(conn)
    assert lock._conn is None


@pytest.mark.asyncio
async def test_aexit_cancelled_during_unlock_releases_conn() -> None:
    """__aexit__'s try/finally already releases the conn on cancel; guard against regressions."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"got": True})
    conn.execute = AsyncMock(side_effect=asyncio.CancelledError())
    pool = _mock_pool(conn)

    lock = AdvisoryLock(pool, key1=1)
    assert await lock.__aenter__() is True
    with pytest.raises(asyncio.CancelledError):
        await lock.__aexit__(None, None, None)

    pool.release.assert_awaited_once_with(conn)
    assert lock._conn is None
