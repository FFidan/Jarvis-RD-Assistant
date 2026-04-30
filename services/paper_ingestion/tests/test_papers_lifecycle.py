"""Tests for paper lifecycle endpoints: trash/restore/delete/bulk/counts.

Phase-A rewrite: aligns with the new paper_user_state schema (state/starred columns)
and the 10-bucket FeedCountsResponse.  Deleted endpoints (save, unsave, dismiss,
archive) and deleted helpers (_assert_confirm_title_matches) are no longer tested here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# conftest.py provides FakeRecord, _make_pool_and_conn, and mock_db fixture.
from paper_ingestion.models import (  # noqa: E402
    BulkActionRequest,
)
from paper_ingestion.routers import papers  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers — mirror conftest._make_pool_and_conn but also support transactions
# ---------------------------------------------------------------------------


def _pool(conn):
    """Wrap a mock conn in an asyncpg-style pool context manager."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _conn_with_txn():
    """Create an AsyncMock connection that also supports nested transactions (SAVEPOINTs)."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    return conn


def _make_pool_and_conn():
    conn = _conn_with_txn()
    pool = _pool(conn)
    return pool, conn


def _mock_request():
    return MagicMock()


# ---------------------------------------------------------------------------
# Hard delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_delete_requires_trash_state():
    """DELETE /papers/{id} returns 409 when paper is NOT in 'trash' state."""
    pool, conn = _make_pool_and_conn()
    # _assert_paper_in_state fetches state via fetchval; None → COALESCE → 'inbox'
    conn.fetchval.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=10,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 409
    assert "trash" in exc_info.value.detail


@pytest.mark.asyncio
async def test_hard_delete_with_trash_state_succeeds():
    """DELETE /papers/{id} returns {"deleted": paper_id} when paper is in 'trash'."""
    pool, conn = _make_pool_and_conn()
    # _assert_paper_in_state: fetchval returns 'trash' → precondition satisfied
    conn.fetchval.return_value = "trash"

    with patch("paper_ingestion.routers.papers.delete_paper_vectors", new_callable=AsyncMock):
        result = await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=12,
            db_pool=pool,
        )

    assert result == {"deleted": 12}
    # The DELETE SQL must have been called
    sql = conn.execute.await_args.args[0]
    assert "DELETE FROM papers" in sql
    assert conn.execute.await_args.args[1] == 12


@pytest.mark.asyncio
async def test_hard_delete_calls_qdrant():
    """DELETE /papers/{id} calls delete_paper_vectors with the correct paper_id."""
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = "trash"

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_delete:
        await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=13,
            db_pool=pool,
        )

    mock_delete.assert_awaited_once_with(13)


# ---------------------------------------------------------------------------
# Bulk action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_action_save_succeeds_for_all():
    """POST /bulk with action=save succeeds for all 3 paper_ids."""
    pool = _make_pool_and_conn()[0]

    result = await papers.bulk_action_papers.__wrapped__(
        _mock_request(),
        body=BulkActionRequest(paper_ids=[1, 2, 3], action="save"),
        db_pool=pool,
    )

    assert set(result["succeeded"]) == {1, 2, 3}
    assert result["failed"] == []


@pytest.mark.asyncio
async def test_bulk_action_mixed_validity():
    """POST /bulk with 2 valid + 1 paper that raises an error yields 2 succeeded, 1 failed."""
    pool = _make_pool_and_conn()[0]

    # Make _apply_bulk_action raise for paper_id=999 only
    original_apply = papers._apply_bulk_action

    async def _selective_fail(c, paper_id, user_id, action):
        if paper_id == 999:
            raise ValueError("paper 999 not found")
        return await original_apply(c, paper_id, user_id, action)

    with patch.object(papers, "_apply_bulk_action", side_effect=_selective_fail):
        result = await papers.bulk_action_papers.__wrapped__(
            _mock_request(),
            body=BulkActionRequest(paper_ids=[1, 2, 999], action="save"),
            db_pool=pool,
        )

    assert set(result["succeeded"]) == {1, 2}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["paper_id"] == 999
    assert "not found" in result["failed"][0]["error"]


@pytest.mark.asyncio
async def test_bulk_action_savepoint_isolation():
    """BULK-TXN-001: failure of paper at index 1 does NOT cascade-fail papers at index 0 or 2.

    The nested asyncpg transaction (SAVEPOINT) must roll back only the failing
    paper's work, leaving the outer transaction alive for subsequent papers.
    """
    pool = _make_pool_and_conn()[0]

    failing_id = 200

    async def _fail_at_200(c, paper_id, user_id, action):
        del c, user_id, action  # signature matches _apply_bulk_action; only paper_id used
        if paper_id == failing_id:
            raise RuntimeError("forced savepoint test failure")
        # Success for others — just pass

    with patch.object(papers, "_apply_bulk_action", side_effect=_fail_at_200):
        result = await papers.bulk_action_papers.__wrapped__(
            _mock_request(),
            body=BulkActionRequest(paper_ids=[100, failing_id, 300], action="save"),
            db_pool=pool,
        )

    # Papers at index 0 (100) and index 2 (300) must have succeeded
    assert 100 in result["succeeded"], "paper 100 (index 0) must succeed"
    assert 300 in result["succeeded"], "paper 300 (index 2) must succeed"
    # The failing paper must appear in failed
    assert len(result["failed"]) == 1
    assert result["failed"][0]["paper_id"] == failing_id


# ---------------------------------------------------------------------------
# Feed counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_counts_basic():
    """GET /feed/counts returns FeedCountsResponse with correct 10-bucket field names."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "inbox": 3,
        "library": 5,
        "reading_list": 2,
        "reading": 1,
        "done": 2,
        "starred": 2,
        "trash": 1,
        "active": 6,
        "kept": 5,
        "all_non_trash": 11,
    }

    result = await papers.get_feed_counts.__wrapped__(
        _mock_request(),
        db_pool=pool,
    )

    assert result.inbox == 3
    assert result.library == 5
    assert result.reading_list == 2
    assert result.reading == 1
    assert result.done == 2
    assert result.starred == 2
    assert result.trash == 1
    assert result.active == 6
    assert result.kept == 5
    assert result.all_non_trash == 11


# ---------------------------------------------------------------------------
# Hard delete — A1.1 / ordering + rollback guarantees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_delete_reorders_qdrant_after_db_commit():
    """A1.1: DELETE FROM papers executes BEFORE delete_paper_vectors (outside transaction)."""
    pool, conn = _make_pool_and_conn()
    # _assert_paper_in_state: paper is in 'trash'
    conn.fetchval.return_value = "trash"

    call_order: list[str] = []

    async def _fake_execute(sql, *args):
        del args  # asyncpg .execute signature compat; values unused in this stub
        if "DELETE FROM papers" in sql:
            call_order.append("db_delete")

    async def _fake_delete_vectors(paper_id):
        del paper_id  # signature compat; only call ordering matters for this test
        call_order.append("qdrant_delete")

    conn.execute.side_effect = _fake_execute

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        side_effect=_fake_delete_vectors,
    ):
        result = await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=50,
            db_pool=pool,
        )

    assert result == {"deleted": 50}
    assert call_order == ["db_delete", "qdrant_delete"], (
        f"Expected db_delete before qdrant_delete, got: {call_order}"
    )


@pytest.mark.asyncio
async def test_hard_delete_db_rollback_does_not_call_qdrant():
    """A1.1: If DELETE FROM papers raises, delete_paper_vectors must NOT be called."""
    import asyncpg as _asyncpg

    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = "trash"
    conn.execute.side_effect = _asyncpg.PostgresError("simulated DB failure")

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_delete:
        with pytest.raises(_asyncpg.PostgresError):
            await papers.hard_delete_paper.__wrapped__(
                _mock_request(),
                paper_id=51,
                db_pool=pool,
            )

    mock_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_qdrant_failure_logs_orphan():
    """A1.1: If delete_paper_vectors raises after DB delete, logger.exception is called with 'orphans'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = "trash"

    async def _fail_vectors(paper_id):
        del paper_id  # signature compat; failure path doesn't depend on the id
        raise RuntimeError("Qdrant connection refused")

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        side_effect=_fail_vectors,
    ):
        with patch.object(papers.logger, "exception") as mock_exc:
            result = await papers.hard_delete_paper.__wrapped__(
                _mock_request(),
                paper_id=52,
                db_pool=pool,
            )

    # DB delete was called (paper is gone)
    delete_calls = [c for c in conn.execute.await_args_list if "DELETE FROM papers" in c.args[0]]
    assert len(delete_calls) == 1, "DELETE FROM papers must have been called once"
    assert delete_calls[0].args[1] == 52

    # logger.exception was called and message contains 'orphans'
    mock_exc.assert_called_once()
    log_msg = mock_exc.call_args.args[0]
    assert "orphans" in log_msg

    # Endpoint still returns success (best-effort Qdrant cleanup)
    assert result == {"deleted": 52}
