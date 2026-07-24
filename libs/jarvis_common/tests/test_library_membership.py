"""Tests for ``jarvis_common.library.is_in_library`` — membership claim check.

Mocked asyncpg connection asserting the behavioural contract: the ``True``/
``False`` mapping of the result set and the bound ``(user_id, paper_id)``
parameters. End-to-end cross-tenant behaviour against the real schema is
covered by the 2-user contract test in
``services/paper_ingestion/tests/contract/test_papers_contract.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncpg
import pytest
from jarvis_common.library import is_in_library


def _make_conn(fetch_return: list[object]) -> AsyncMock:
    """Mock Connection whose ``fetch`` yields ``fetch_return``."""
    conn = AsyncMock(spec=asyncpg.Connection)
    conn.fetch = AsyncMock(return_value=fetch_return)
    return conn


@pytest.mark.asyncio
async def test_is_in_library_true_when_row_present():
    """A matching ``user_library`` row maps to ``True`` and probes the pair."""
    conn = _make_conn([{"?column?": 1}])

    result = await is_in_library(conn, user_id=42, paper_id=7)

    assert result is True
    conn.fetch.assert_awaited_once()
    _, *args = conn.fetch.await_args.args
    assert args == [42, 7]


@pytest.mark.asyncio
async def test_is_in_library_false_when_no_row():
    """An empty result set maps to ``False`` (no membership)."""
    conn = _make_conn([])

    result = await is_in_library(conn, user_id=1, paper_id=2)

    assert result is False
    conn.fetch.assert_awaited_once()
