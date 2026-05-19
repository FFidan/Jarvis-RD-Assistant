"""Shared test fakes for source event / log_event tests.

D4-04: ``_mock_pool`` was defined identically in three source-event test files
(test_source_arxiv_events.py, test_source_s2_events.py,
test_source_openalex_events.py).  Import from here instead.

Note: this is a lightweight *event-capture* pool — it records ``conn.execute``
calls without touching a real database.  It differs from the general-purpose
``make_pool_and_conn`` (jarvis_common.testing) in that it does not wire
``conn.transaction`` or provide fetchval/fetchrow shims, matching the minimal
shape that log_event tests require.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def mock_log_event_pool() -> MagicMock:
    """Return a mock asyncpg pool that records ``execute`` calls.

    Suitable for tests that exercise ``log_event`` emission when a real pool
    is supplied to a source (``db_pool=pool``).
    """
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool
