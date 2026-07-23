"""Regression tests for the persisted paper-visibility boundary.

The canonical bibliography is shared only when a server-owned ingestion path
has persisted public scope. Private rows require explicit caller-library
membership. Discoverer attribution and source labels are not authorization.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from jarvis_common.db_helpers import assert_papers_ownership


def _batch_connection(rows: list[dict[str, object]]) -> AsyncMock:
    """Return a connection whose batch visibility query yields `rows`."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    return conn


@pytest.mark.asyncio
async def test_batch_public_and_library_visible_rows_are_allowed() -> None:
    """A mixed visible batch is normalized and accepted as one query."""
    conn = _batch_connection(
        [
            {"id": 1, "is_visible": True},
            {"id": 2, "is_visible": True},
        ]
    )

    await assert_papers_ownership(conn, [2, 1, 2], user_id=42)

    _, paper_ids, user_id = conn.fetch.await_args.args
    assert paper_ids == [1, 2]
    assert user_id == 42


@pytest.mark.asyncio
async def test_batch_private_row_without_membership_is_403() -> None:
    """One unauthorized private row rejects the whole batch."""
    conn = _batch_connection(
        [
            {"id": 1, "is_visible": True},
            {"id": 2, "is_visible": False},
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        await assert_papers_ownership(conn, [1, 2], user_id=42)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_batch_missing_row_is_404_before_visibility_result() -> None:
    """A missing paper remains a 404 even when another row is unauthorized."""
    conn = _batch_connection([{"id": 2, "is_visible": False}])

    with pytest.raises(HTTPException) as exc_info:
        await assert_papers_ownership(conn, [1, 2], user_id=42)

    assert exc_info.value.status_code == 404
    assert "1" in exc_info.value.detail


@pytest.mark.asyncio
async def test_batch_empty_or_internal_request_skips_database() -> None:
    """Empty and trusted-internal batches make no authorization query."""
    conn = _batch_connection([])

    await assert_papers_ownership(conn, [], user_id=42)
    await assert_papers_ownership(conn, [1], user_id=None)

    conn.fetch.assert_not_awaited()
