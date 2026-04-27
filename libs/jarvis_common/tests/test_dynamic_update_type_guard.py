"""M1 — Runtime type guard for dynamic_update extra_sets.

Verifies that passing non-str elements in extra_sets raises TypeError immediately,
making the "trusted callers only" contract explicit and catchable in tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common.db_helpers import dynamic_update


@pytest.mark.asyncio
async def test_dynamic_update_rejects_non_str_extra_sets():
    """M1: dynamic_update must raise TypeError when extra_sets contains non-str elements."""
    conn = AsyncMock()

    with pytest.raises(TypeError, match="extra_sets must all be str"):
        await dynamic_update(
            conn,
            "topics",
            1,
            updates={"name": "hello"},
            allowed_columns=frozenset({"name"}),
            extra_sets=["valid_fragment = NOW()", 123],  # type: ignore[list-item]
        )

    # Confirm conn was never called — guard fires before any DB access
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_accepts_all_str_extra_sets():
    """M1 positive path: all-str extra_sets are accepted and appended to SET clause."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": 1, "name": "hello", "updated_at": None}

    await dynamic_update(
        conn,
        "topics",
        1,
        updates={"name": "hello"},
        allowed_columns=frozenset({"name"}),
        extra_sets=["updated_at = NOW()"],
    )

    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "updated_at = NOW()" in sql
