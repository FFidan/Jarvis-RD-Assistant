"""Tests for paper lifecycle endpoints: save/unsave/dismiss/restore/delete/bulk/counts/archive.

WS8-B3.1 — Sprint 8 lifecycle endpoint coverage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# conftest.py provides FakeRecord, _make_pool_and_conn, and mock_db fixture.
from paper_ingestion.models import (  # noqa: E402
    ArchiveRequest,
    BulkActionRequest,
    DismissRequest,
    HardDeleteRequest,
    SaveRequest,
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
# Save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_writes_saved_true():
    """PUT /save sets saved=TRUE; starred stays False when body.star=False."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 1}

    result = await papers.save_paper.__wrapped__(
        _mock_request(),
        paper_id=1,
        body=SaveRequest(star=False),
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 1}
    # _upsert_user_state was called: assert saved=True appears in SQL
    sql = conn.execute.await_args.args[0]
    assert "saved" in sql
    # 'starred' should NOT appear as an extra field when star=False
    # (only saved=True is passed, not starred)
    # The SQL is built dynamically from columns; verify saved is the only extra column
    positional = conn.execute.await_args.args
    # positional: (sql, paper_id, user_id, True) — saved=True is the last arg
    assert True in positional


@pytest.mark.asyncio
async def test_save_with_star_writes_both():
    """PUT /save with {star: true} sets both saved=TRUE and starred=TRUE."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 2}

    result = await papers.save_paper.__wrapped__(
        _mock_request(),
        paper_id=2,
        body=SaveRequest(star=True),
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 2}
    sql = conn.execute.await_args.args[0]
    assert "saved" in sql
    assert "starred" in sql
    positional = conn.execute.await_args.args
    # Both True values (saved=True, starred=True) must appear
    assert positional.count(True) >= 2


@pytest.mark.asyncio
async def test_save_idempotent():
    """Calling /save twice should both succeed with ok status."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 3}

    result1 = await papers.save_paper.__wrapped__(
        _mock_request(),
        paper_id=3,
        body=SaveRequest(star=False),
        db_pool=pool,
    )
    result2 = await papers.save_paper.__wrapped__(
        _mock_request(),
        paper_id=3,
        body=SaveRequest(star=False),
        db_pool=pool,
    )

    assert result1 == {"status": "ok", "paper_id": 3}
    assert result2 == {"status": "ok", "paper_id": 3}
    # Both calls should have executed the upsert SQL
    assert conn.execute.await_count == 2
    # Both upsert SQLs contain ON CONFLICT (idempotent upsert)
    for call in conn.execute.await_args_list:
        assert "ON CONFLICT" in call.args[0]


# ---------------------------------------------------------------------------
# Unsave
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsave_preserves_star():
    """PUT /unsave sets saved=FALSE; the SQL does NOT touch the starred column."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 4}

    result = await papers.unsave_paper.__wrapped__(
        _mock_request(),
        paper_id=4,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 4}
    sql = conn.execute.await_args.args[0]
    # saved must be in the SQL
    assert "saved" in sql
    # starred must NOT appear (preserved via ON CONFLICT DO UPDATE — not explicitly set)
    assert "starred" not in sql


# ---------------------------------------------------------------------------
# Dismiss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_writes_dismissed_and_pref():
    """PUT /dismiss sets dismissed=TRUE and preference='down'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 5}

    result = await papers.dismiss_paper.__wrapped__(
        _mock_request(),
        paper_id=5,
        body=DismissRequest(also_zotero=False),
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 5}
    sql = conn.execute.await_args.args[0]
    assert "dismissed" in sql
    assert "preference" in sql
    positional = conn.execute.await_args.args
    # 'down' string must appear in positional args (preference value)
    assert "down" in positional


@pytest.mark.asyncio
async def test_dismiss_preserves_saved():
    """PUT /dismiss does NOT include saved in the upsert fields (so saved is preserved)."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 6}

    result = await papers.dismiss_paper.__wrapped__(
        _mock_request(),
        paper_id=6,
        body=DismissRequest(also_zotero=False),
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 6}
    sql = conn.execute.await_args.args[0]
    # The dismiss upsert sets dismissed and preference — saved is not mentioned
    # (ON CONFLICT DO UPDATE only touches the columns that are listed in updates)
    assert "dismissed" in sql
    assert "saved" not in sql


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_clears_dismissed():
    """PUT /restore sets dismissed=FALSE and preference='none'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 7}

    result = await papers.restore_paper.__wrapped__(
        _mock_request(),
        paper_id=7,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 7}
    sql = conn.execute.await_args.args[0]
    assert "dismissed" in sql
    assert "preference" in sql
    positional = conn.execute.await_args.args
    assert False in positional  # dismissed=False
    assert "none" in positional  # preference='none'


@pytest.mark.asyncio
async def test_restore_preserves_saved():
    """PUT /restore preserves saved=TRUE so previously-saved papers return to Library.

    When a paper is dismissed, it goes to Trash. Restore clears dismissed=FALSE
    and preference='none', but does NOT touch saved — so if the paper was saved
    before dismissal, it returns to Library (saved=TRUE); if never saved,
    it returns to Inbox (saved=FALSE).
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 8}

    result = await papers.restore_paper.__wrapped__(
        _mock_request(),
        paper_id=8,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 8}
    sql = conn.execute.await_args.args[0]
    # restore must set dismissed and preference
    assert "dismissed" in sql
    assert "preference" in sql
    # Crucially: saved must NOT appear in the SQL (so it's preserved)
    assert "saved" not in sql


# ---------------------------------------------------------------------------
# Hard delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_delete_requires_dismissed():
    """DELETE /papers/{id} returns 409 when paper is NOT in Trash."""
    pool, conn = _make_pool_and_conn()
    # Paper exists; fetchrow returns a row so ownership check passes.
    # fetchval returns None (no user_state row → not dismissed).
    conn.fetchval.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=10,
            body=HardDeleteRequest(confirm_title="Some Paper", also_zotero=False),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 409
    assert "Trash" in exc_info.value.detail


@pytest.mark.asyncio
async def test_hard_delete_requires_title_match():
    """DELETE /papers/{id} returns 400 when confirm_title does not match paper title."""
    pool, conn = _make_pool_and_conn()
    # fetchval calls: first = dismissed check (True), second = title fetch ("Real Title")
    conn.fetchval.side_effect = [True, "Real Title"]

    with pytest.raises(HTTPException) as exc_info:
        await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=11,
            body=HardDeleteRequest(confirm_title="Wrong Title", also_zotero=False),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 400
    assert "confirm_title" in exc_info.value.detail


@pytest.mark.asyncio
async def test_hard_delete_with_correct_title_succeeds():
    """DELETE /papers/{id} returns {"deleted": paper_id} on happy path."""
    pool, conn = _make_pool_and_conn()
    # fetchval: dismissed=True, title="Correct Title"
    conn.fetchval.side_effect = [True, "Correct Title"]

    with patch("paper_ingestion.routers.papers.delete_paper_vectors", new_callable=AsyncMock):
        result = await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=12,
            body=HardDeleteRequest(confirm_title="Correct Title", also_zotero=False),
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
    conn.fetchval.side_effect = [True, "The Title"]

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_delete:
        await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=13,
            body=HardDeleteRequest(confirm_title="The Title", also_zotero=False),
            db_pool=pool,
        )

    mock_delete.assert_awaited_once_with(13)


# ---------------------------------------------------------------------------
# Bulk action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_action_save_succeeds_for_all():
    """POST /bulk with action=save succeeds for all 3 paper_ids."""
    pool, conn = _make_pool_and_conn()

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
    pool, conn = _make_pool_and_conn()

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
    pool, conn = _make_pool_and_conn()

    failing_id = 200

    async def _fail_at_200(c, paper_id, user_id, action):
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
    """GET /feed/counts returns FeedCountsResponse with correct field names."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "inbox": 1,
        "library": 2,
        "starred": 1,
        "archived": 1,
        "reading": 0,
        "trash": 1,
        "all_active": 4,
    }

    result = await papers.get_feed_counts.__wrapped__(
        _mock_request(),
        db_pool=pool,
    )

    assert result.inbox == 1
    assert result.library == 2
    assert result.starred == 1
    assert result.archived == 1
    assert result.reading == 0
    assert result.trash == 1
    assert result.all_active == 4


# ---------------------------------------------------------------------------
# Archive precondition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_requires_saved():
    """PUT /archive returns 409 when the paper has not been saved first."""
    pool, conn = _make_pool_and_conn()
    # fetchrow: paper exists
    conn.fetchrow.return_value = {"id": 20}
    # fetchval: saved=False (not in Library)
    conn.fetchval.return_value = False

    with pytest.raises(HTTPException) as exc_info:
        await papers.archive_paper.__wrapped__(
            _mock_request(),
            paper_id=20,
            body=ArchiveRequest(archive=True),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 409
    assert "Save before archiving" in exc_info.value.detail


@pytest.mark.asyncio
async def test_archive_after_save_works():
    """PUT /archive succeeds when the paper is already saved."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 21}
    # fetchval: saved=True → archive precondition satisfied
    conn.fetchval.return_value = True

    result = await papers.archive_paper.__wrapped__(
        _mock_request(),
        paper_id=21,
        body=ArchiveRequest(archive=True),
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 21}
    sql = conn.execute.await_args.args[0]
    assert "archived" in sql


@pytest.mark.asyncio
async def test_unarchive_works():
    """PUT /archive with {archive: false} succeeds without any saved precondition."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 22}
    # fetchval should NOT be called for unarchive (archive=False skips the check)

    result = await papers.archive_paper.__wrapped__(
        _mock_request(),
        paper_id=22,
        body=ArchiveRequest(archive=False),
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 22}
    sql = conn.execute.await_args.args[0]
    assert "archived" in sql
    # Crucially: fetchval was not called (no saved-state check for unarchive)
    conn.fetchval.assert_not_awaited()


# ---------------------------------------------------------------------------
# Hard delete — A1.1 / A1.2 new test cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_delete_reorders_qdrant_after_db_commit():
    """A1.1: DELETE FROM papers executes BEFORE delete_paper_vectors (outside transaction)."""
    pool, conn = _make_pool_and_conn()
    # dismissed=True, title matches
    conn.fetchval.side_effect = [True, "Order Test"]

    call_order: list[str] = []

    async def _fake_execute(sql, *args):
        if "DELETE FROM papers" in sql:
            call_order.append("db_delete")

    async def _fake_delete_vectors(paper_id):
        call_order.append("qdrant_delete")

    conn.execute.side_effect = _fake_execute

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        side_effect=_fake_delete_vectors,
    ):
        result = await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=50,
            body=HardDeleteRequest(confirm_title="Order Test", also_zotero=False),
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
    conn.fetchval.side_effect = [True, "Rollback Test"]
    conn.execute.side_effect = _asyncpg.PostgresError("simulated DB failure")

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_delete:
        with pytest.raises(_asyncpg.PostgresError):
            await papers.hard_delete_paper.__wrapped__(
                _mock_request(),
                paper_id=51,
                body=HardDeleteRequest(confirm_title="Rollback Test", also_zotero=False),
                db_pool=pool,
            )

    mock_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_qdrant_failure_logs_orphan():
    """A1.1: If delete_paper_vectors raises after DB delete, logger.exception is called with 'orphans'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchval.side_effect = [True, "Qdrant Fail"]

    async def _fail_vectors(paper_id):
        raise RuntimeError("Qdrant connection refused")

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        side_effect=_fail_vectors,
    ):
        with patch.object(papers.logger, "exception") as mock_exc:
            result = await papers.hard_delete_paper.__wrapped__(
                _mock_request(),
                paper_id=52,
                body=HardDeleteRequest(confirm_title="Qdrant Fail", also_zotero=False),
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


@pytest.mark.asyncio
async def test_hard_delete_confirm_title_trim():
    """A1.2: _assert_confirm_title_matches passes when title has leading/trailing whitespace."""
    conn = AsyncMock()
    conn.fetchval.return_value = "  Foo  "

    # Should NOT raise — trimmed values match
    await papers._assert_confirm_title_matches(conn, paper_id=1, confirm_title="Foo")


@pytest.mark.asyncio
async def test_hard_delete_confirm_title_case_mismatch():
    """A1.2: _assert_confirm_title_matches raises 400 when titles differ in case."""
    conn = AsyncMock()
    conn.fetchval.return_value = "foo"

    with pytest.raises(HTTPException) as exc_info:
        await papers._assert_confirm_title_matches(conn, paper_id=1, confirm_title="Foo")

    assert exc_info.value.status_code == 400
    assert "confirm_title" in exc_info.value.detail
