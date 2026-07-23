"""Tests for the centralized single-paper visibility guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def _connection(result: dict[str, object] | None) -> AsyncMock:
    """Return a connection whose visibility query yields `result`."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=result)
    return conn


@pytest.mark.asyncio
async def test_missing_paper_is_404() -> None:
    """An absent row remains distinguishable from an unauthorized row."""
    from jarvis_common.db_helpers import assert_paper_ownership

    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(_connection(None), paper_id=999, user_id=42)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_trusted_internal_mode_skips_database_check() -> None:
    """A `None` caller preserves the explicit trusted-internal bypass."""
    from jarvis_common.db_helpers import assert_paper_ownership

    conn = _connection(None)
    await assert_paper_ownership(conn, paper_id=1, user_id=None)

    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_paper_is_visible_without_library_membership() -> None:
    """Persisted public scope grants access independently of audit attribution."""
    from jarvis_common.db_helpers import assert_paper_ownership

    conn = _connection({"id": 1, "is_visible": True})
    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_private_library_paper_is_visible() -> None:
    """The SQL predicate's membership branch grants an explicitly shelved paper."""
    from jarvis_common.db_helpers import assert_paper_ownership

    conn = _connection({"id": 1, "is_visible": True})
    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_private_discoverer_without_library_membership_is_rejected() -> None:
    """Audit attribution alone must not grant access to a private paper."""
    from jarvis_common.db_helpers import assert_paper_ownership

    conn = _connection({"id": 1, "is_visible": False})
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=1, user_id=42)

    assert exc_info.value.status_code == 403


def test_guard_is_exported_from_jarvis_common() -> None:
    """The guard remains importable from the package compatibility surface."""
    from jarvis_common import assert_paper_ownership  # noqa: F401
