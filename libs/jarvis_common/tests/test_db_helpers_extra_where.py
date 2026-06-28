"""Unit tests for dynamic_update extra_where optional kwarg.

Verifies that the new ``extra_where`` param is purely additive:
  - When supplied, AND-predicates the WHERE clause and binds the value.
  - When omitted (default None), SQL is byte-identical to the prior behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common.db_helpers import dynamic_update


@pytest.mark.asyncio
async def test_dynamic_update_extra_where_appends_and_predicate():
    """extra_where=("user_id", 7) appends AND "user_id" = $N and binds the value."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": 5, "user_note": "hello"}

    await dynamic_update(
        conn,
        "paper_notes",
        5,
        updates={"user_note": "hello"},
        allowed_columns=frozenset({"user_note"}),
        extra_where=("user_id", 7),
    )

    params = conn.fetchrow.await_args.args[1:]
    # extra_where binds its value as the final param, after record_id ($1) and
    # the SET value ($2): (record_id, set_value, user_id). The trailing bound
    # param — which the default path omits — proves the predicate was appended.
    assert params == (5, "hello", 7), f"extra_where must bind user_id last; got: {params}"


@pytest.mark.asyncio
async def test_dynamic_update_no_extra_where_produces_original_where():
    """Omitting extra_where yields WHERE id = $1 with no AND clause (backward-compat)."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": 5, "user_note": "hello"}

    await dynamic_update(
        conn,
        "paper_notes",
        5,
        updates={"user_note": "hello"},
        allowed_columns=frozenset({"user_note"}),
    )

    params = conn.fetchrow.await_args.args[1:]
    # Default path binds only (record_id, set_value) — byte-identical to the
    # prior behaviour, with no extra predicate param.
    assert params == (5, "hello"), f"Expected (record_id, value) params; got: {params}"
