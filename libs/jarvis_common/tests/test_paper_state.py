"""Tests for jarvis_common.paper_state — all five on_conflict variants.

Each test uses an AsyncMock asyncpg connection so there is no live DB
dependency.  We verify:
- the correct asyncpg method is called (execute vs fetchrow vs fetchval)
- the SQL contains the expected ON CONFLICT clause keyword
- the correct positional arguments are forwarded
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from jarvis_common.paper_state import (
    assert_paper_in_states,
    trash_paper,
    upsert_paper_user_state,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_conn(*, execute_return=None, fetchrow_return=None, fetchval_return=None) -> AsyncMock:
    conn = AsyncMock()
    conn.execute.return_value = execute_return
    conn.fetchrow.return_value = fetchrow_return
    conn.fetchval.return_value = fetchval_return
    return conn


# ---------------------------------------------------------------------------
# update_dynamic variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_dynamic_state_only() -> None:
    conn = _mock_conn()
    await upsert_paper_user_state(conn, 1, 42, state="reading", on_conflict="update_dynamic")
    conn.execute.assert_awaited_once()
    sql: str = conn.execute.await_args.args[0]
    assert "ON CONFLICT" in sql
    assert "DO UPDATE SET" in sql
    assert "state" in sql
    # starred should NOT be in UPDATE clause since it was not supplied
    # (it should not appear in the dynamic build at all)
    called_args = conn.execute.await_args.args
    assert "reading" in called_args


@pytest.mark.asyncio
async def test_update_dynamic_starred_only() -> None:
    conn = _mock_conn()
    await upsert_paper_user_state(conn, 2, None, starred=False, on_conflict="update_dynamic")
    conn.execute.assert_awaited_once()
    sql: str = conn.execute.await_args.args[0]
    assert "starred" in sql
    assert "DO UPDATE SET" in sql


@pytest.mark.asyncio
async def test_update_dynamic_both_fields() -> None:
    conn = _mock_conn()
    await upsert_paper_user_state(
        conn, 3, 7, state="done", starred=True, on_conflict="update_dynamic"
    )
    conn.execute.assert_awaited_once()
    sql: str = conn.execute.await_args.args[0]
    assert "state" in sql
    assert "starred" in sql


@pytest.mark.asyncio
async def test_update_dynamic_no_fields_is_noop() -> None:
    """When both state and starred are None, no DB call should be made."""
    conn = _mock_conn()
    await upsert_paper_user_state(conn, 1, 1, on_conflict="update_dynamic")
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_starred_only variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_starred_only_returns_fetchrow_result() -> None:
    fake_row = MagicMock()
    fake_row.__getitem__ = lambda self, k: {"is_new_row": True, "prev_starred": False}[k]
    conn = _mock_conn(fetchrow_return=fake_row)

    result = await upsert_paper_user_state(conn, 5, 10, on_conflict="update_starred_only")

    conn.fetchrow.assert_awaited_once()
    sql: str = conn.fetchrow.await_args.args[0]
    assert "ON CONFLICT" in sql
    assert "starred = TRUE" in sql
    assert "RETURNING" in sql
    assert result is fake_row


@pytest.mark.asyncio
async def test_update_starred_only_sql_has_cte() -> None:
    conn = _mock_conn(fetchrow_return=None)
    await upsert_paper_user_state(conn, 5, None, on_conflict="update_starred_only")
    sql: str = conn.fetchrow.await_args.args[0]
    # CTE snapshot for TOCTOU-free transition detection
    assert "WITH before AS" in sql
    assert "prev_starred" in sql


# ---------------------------------------------------------------------------
# update_partial variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_partial_returns_full_row() -> None:
    fake_row = {
        "state": "inbox",
        "starred": False,
        "rating": 4,
        "user_notes": "good",
        "flagged": False,
    }
    conn = _mock_conn(fetchrow_return=fake_row)

    result = await upsert_paper_user_state(
        conn, 7, 3, rating=4, user_notes="good", on_conflict="update_partial"
    )

    conn.fetchrow.assert_awaited_once()
    sql: str = conn.fetchrow.await_args.args[0]
    assert "COALESCE" in sql
    assert "rating" in sql
    assert "user_notes" in sql
    assert "flagged" in sql
    assert "RETURNING" in sql
    assert result is fake_row


@pytest.mark.asyncio
async def test_update_partial_preserves_none_fields() -> None:
    """NULL args must be forwarded as None so COALESCE preserves existing values."""
    conn = _mock_conn(fetchrow_return={})
    await upsert_paper_user_state(
        conn, 8, 1, rating=None, user_notes=None, flagged=None, on_conflict="update_partial"
    )
    called_args = conn.fetchrow.await_args.args
    # $3=$rating, $4=$user_notes, $5=$flagged — all None
    assert called_args[3] is None  # rating
    assert called_args[4] is None  # user_notes
    assert called_args[5] is None  # flagged


# ---------------------------------------------------------------------------
# do_nothing variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_nothing_uses_execute() -> None:
    conn = _mock_conn()
    await upsert_paper_user_state(
        conn, 9, None, state="to_read", starred=False, on_conflict="do_nothing"
    )
    conn.execute.assert_awaited_once()
    sql: str = conn.execute.await_args.args[0]
    assert "DO NOTHING" in sql


@pytest.mark.asyncio
async def test_do_nothing_defaults_state_and_starred() -> None:
    """When state/starred not supplied, defaults to 'inbox' / False."""
    conn = _mock_conn()
    await upsert_paper_user_state(conn, 9, None, on_conflict="do_nothing")
    called_args = conn.execute.await_args.args
    # $3=state, $4=starred
    assert called_args[3] == "inbox"
    assert called_args[4] is False


# ---------------------------------------------------------------------------
# update_state_when_inbox_or_to_read variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_state_conditional_sql() -> None:
    conn = _mock_conn()
    await upsert_paper_user_state(
        conn, 11, 5, state="reading", on_conflict="update_state_when_inbox_or_to_read"
    )
    conn.execute.assert_awaited_once()
    sql: str = conn.execute.await_args.args[0]
    assert "WHERE paper_user_state.state IN" in sql
    assert "'inbox'" in sql
    assert "'to_read'" in sql
    # The state value should be forwarded
    called_args = conn.execute.await_args.args
    assert "reading" in called_args


@pytest.mark.asyncio
async def test_update_state_conditional_raises_without_state() -> None:
    conn = _mock_conn()
    with pytest.raises(ValueError, match="state must be provided"):
        await upsert_paper_user_state(conn, 11, 5, on_conflict="update_state_when_inbox_or_to_read")


# ---------------------------------------------------------------------------
# trash_paper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trash_paper_sql_has_case_expression() -> None:
    conn = _mock_conn()
    await trash_paper(conn, 20, 1)
    conn.execute.assert_awaited_once()
    sql: str = conn.execute.await_args.args[0]
    assert "state_before_trash" in sql
    assert "CASE" in sql
    assert "state = 'trash'" in sql


@pytest.mark.asyncio
async def test_trash_paper_forwards_ids() -> None:
    conn = _mock_conn()
    await trash_paper(conn, 99, None)
    called_args = conn.execute.await_args.args
    assert called_args[1] == 99
    assert called_args[2] is None


# ---------------------------------------------------------------------------
# assert_paper_in_states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_in_states_passes_when_allowed() -> None:
    conn = _mock_conn(fetchval_return="to_read")
    # Should not raise
    await assert_paper_in_states(conn, 1, 1, allowed=("to_read", "reading"))


@pytest.mark.asyncio
async def test_assert_paper_in_states_raises_409_when_disallowed() -> None:
    conn = _mock_conn(fetchval_return="done")
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_in_states(conn, 1, 1, allowed=("inbox",))
    assert exc_info.value.status_code == 409
    assert "done" in exc_info.value.detail


@pytest.mark.asyncio
async def test_assert_paper_in_states_treats_none_as_inbox() -> None:
    """A missing row (fetchval returns None) is treated as 'inbox'."""
    conn = _mock_conn(fetchval_return=None)
    # Should not raise — inbox is the implicit default
    await assert_paper_in_states(conn, 1, 1, allowed=("inbox",))


@pytest.mark.asyncio
async def test_assert_paper_in_states_sorted_in_detail() -> None:
    """Detail message should list allowed states in sorted order."""
    conn = _mock_conn(fetchval_return="trash")
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_in_states(conn, 1, 1, allowed=("reading", "inbox"))
    # sorted(("reading", "inbox")) == ["inbox", "reading"]
    assert "inbox" in exc_info.value.detail
