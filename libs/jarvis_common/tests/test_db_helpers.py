"""NEW-M10 — Regex whitelist validation for dynamic_update extra_sets.

Verifies that the _EXTRA_SET_RE whitelist accepts only the three safe forms
(col = NOW(), col = NULL, col = $N) and rejects arbitrary SQL fragments.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common.db_helpers import dynamic_update

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn_returning(record: dict) -> AsyncMock:
    """Return a mock asyncpg connection whose fetchrow resolves to *record*."""
    conn = AsyncMock()
    conn.fetchrow.return_value = record
    return conn


_BASE_RECORD = {"id": 1, "name": "x", "updated_at": None, "completed_at": None, "flag": None}

_CALL_KWARGS: dict = dict(
    table="topics",
    record_id=1,
    updates={"name": "x"},
    allowed_columns=frozenset({"name"}),
)


# ---------------------------------------------------------------------------
# Positive cases — all accepted by the whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_update_accepts_now():
    """'updated_at = NOW()' must be accepted without error."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(conn, extra_sets=["updated_at = NOW()"], **_CALL_KWARGS)
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "updated_at = NOW()" in sql


@pytest.mark.asyncio
async def test_dynamic_update_accepts_null():
    """'flag = NULL' must be accepted without error."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(conn, extra_sets=["flag = NULL"], **_CALL_KWARGS)
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "flag = NULL" in sql


@pytest.mark.asyncio
async def test_dynamic_update_accepts_placeholder():
    """'col = $5' (positional placeholder) must be accepted without error."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(conn, extra_sets=["flag = $5"], **_CALL_KWARGS)
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "flag = $5" in sql


@pytest.mark.asyncio
async def test_dynamic_update_accepts_multiple_valid_fragments():
    """Multiple valid fragments in extra_sets should all be accepted."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(
        conn,
        extra_sets=["updated_at = NOW()", "completed_at = NULL"],
        **_CALL_KWARGS,
    )
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "updated_at = NOW()" in sql
    assert "completed_at = NULL" in sql


# ---------------------------------------------------------------------------
# Negative cases — all rejected by the whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_update_rejects_literal():
    """A string literal value like 'col = \\'literal_string\\'' must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = 'literal_string'"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_subquery():
    """A subquery fragment 'col = (SELECT 1)' must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = (SELECT 1)"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_function_call():
    """An arbitrary function call 'col = some_func()' must raise ValueError (only NOW() is whitelisted)."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = some_func()"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_semicolon_injection():
    """SQL injection via semicolon must be rejected."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = NOW(); DROP TABLE papers --"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_subquery_assignment():
    """'col = (SELECT password FROM secrets)' must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = (SELECT password FROM secrets)"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_numeric_literal():
    """'col = 42' (bare numeric) must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = 42"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_error_mentions_bad_fragment():
    """The ValueError message must name the offending fragment for easy debugging."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match=r"col = 'bad'"):
        await dynamic_update(
            conn,
            extra_sets=["col = 'bad'"],
            **_CALL_KWARGS,
        )
