"""Unit test: string "false" legacy row correctly DISABLES recommendations.

Before the fix, bool("false") == True caused a string "false" value in
user_config to enable recommendations instead of disabling them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.ingestion.recommender import _read_weights


@pytest.mark.asyncio
async def test_string_false_disables_recommendations() -> None:
    """A string 'false' row must yield enabled=False, not True."""
    pool, conn = make_pool_and_conn()
    conn.fetch = AsyncMock(
        return_value=[
            {"key": "recommendation.enabled", "value": "false", "user_id": None},
        ]
    )
    _, _, enabled = await _read_weights(conn, user_id=1)
    assert enabled is False, (
        "string 'false' must disable recommendations; got enabled=True (the pre-fix bug)"
    )


@pytest.mark.asyncio
async def test_string_true_enables_recommendations() -> None:
    """A string 'true' row must yield enabled=True."""
    pool, conn = make_pool_and_conn()
    conn.fetch = AsyncMock(
        return_value=[
            {"key": "recommendation.enabled", "value": "true", "user_id": None},
        ]
    )
    _, _, enabled = await _read_weights(conn, user_id=1)
    assert enabled is True


@pytest.mark.asyncio
async def test_bool_false_disables_recommendations() -> None:
    """A real bool False must still yield enabled=False (no regression)."""
    pool, conn = make_pool_and_conn()
    conn.fetch = AsyncMock(
        return_value=[
            {"key": "recommendation.enabled", "value": False, "user_id": None},
        ]
    )
    _, _, enabled = await _read_weights(conn, user_id=1)
    assert enabled is False


@pytest.mark.asyncio
async def test_missing_enabled_defaults_to_true() -> None:
    """When recommendation.enabled is absent the default is True."""
    pool, conn = make_pool_and_conn()
    conn.fetch = AsyncMock(return_value=[])
    _, _, enabled = await _read_weights(conn, user_id=1)
    assert enabled is True
