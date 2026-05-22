"""Tests for paper lifecycle endpoints: trash/restore/delete/bulk/counts.

Phase-A rewrite: aligns with the new paper_user_state schema (state/starred columns)
and the 10-bucket FeedCountsResponse.  Deleted endpoints (save, unsave, dismiss,
archive) and deleted helpers (_assert_confirm_title_matches) are no longer tested here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport

# conftest.py provides FakeRecord, _make_pool_and_conn, and mock_db fixture.
from paper_ingestion.models import (  # noqa: E402
    BulkActionRequest,
)
from paper_ingestion.routers import papers  # noqa: E402

from tests.conftest import _make_pool_and_conn


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

    # WS-CROSS-USER: ownership now always runs; this test asserts the trash
    # state precondition (ownership covered elsewhere) — pass it through.
    with (
        patch(
            "paper_ingestion.papers_service.assert_paper_ownership",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await papers.hard_delete_paper.__wrapped__(
            _mock_request(),
            paper_id=10,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 409
    assert "trash" in exc_info.value.detail


@pytest.mark.asyncio
async def test_hard_delete_calls_qdrant():
    """DELETE /papers/{id} calls delete_paper_vectors with the correct paper_id."""
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = "trash"

    with patch(
        "paper_ingestion.papers_service.delete_paper_vectors",
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


# Collapsed (Phase C): test_bulk_action_save_succeeds_for_all
# Survivor: test_papers_contract.py::test_a84_bulk_action_transitions_state_for_owner
# Behavioral outcome (succeeded=[1,2,3], failed=[]) covered by contract A84 with real DB.


@pytest.mark.asyncio
async def test_bulk_action_mixed_validity():
    """POST /bulk with 2 valid + 1 paper that raises an error yields 2 succeeded, 1 failed."""
    pool = _make_pool_and_conn()[0]

    # Make _apply_bulk_action raise for paper_id=999 only
    original_apply = papers._apply_bulk_action

    async def _selective_fail(c, paper_id, user_id, action, **_kwargs):
        if paper_id == 999:
            raise ValueError("paper 999 not found")
        return await original_apply(c, paper_id, user_id, action, **_kwargs)

    with patch.object(papers, "_apply_bulk_action", side_effect=_selective_fail):
        result = await papers.bulk_action_papers.__wrapped__(
            _mock_request(),
            body=BulkActionRequest(paper_ids=[1, 2, 999], action="save"),
            db_pool=pool,
        )

    assert set(result["succeeded"]) == {1, 2}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["paper_id"] == 999
    # Error must be a safe enum code, not a raw exception string
    assert result["failed"][0]["error"] == "invalid_action"


# ---------------------------------------------------------------------------
# Bulk hard_delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_hard_delete_succeeds_on_trash_papers():
    """POST /bulk with action='hard_delete' on 3 trashed papers returns all 3 in succeeded."""
    pool, conn = _make_pool_and_conn()
    # _assert_paper_in_state calls fetchval; return "trash" so precondition passes for all
    conn.fetchval.return_value = "trash"

    with patch(
        "paper_ingestion.papers_service.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_delete:
        result = await papers.bulk_action_papers.__wrapped__(
            _mock_request(),
            body=BulkActionRequest(paper_ids=[10, 20, 30], action="hard_delete"),
            db_pool=pool,
        )

    assert set(result["succeeded"]) == {10, 20, 30}
    assert result["failed"] == []
    # Qdrant cleanup must have been called once per paper
    assert mock_delete.await_count == 3


@pytest.mark.asyncio
async def test_bulk_hard_delete_rejects_non_trash_papers():
    """POST /bulk with action='hard_delete' on mixed states: only trash papers succeed."""
    pool, conn = _make_pool_and_conn()

    # paper 10 and 20 are in trash; paper 30 is in inbox (fetchval returns "inbox")
    trash_ids = {10, 20}
    inbox_id = 30

    async def _fetchval_side_effect(_query, paper_id, _user_id):
        if paper_id in trash_ids:
            return "trash"
        return "inbox"  # triggers _assert_paper_in_state to raise HTTPException

    conn.fetchval.side_effect = _fetchval_side_effect

    with patch(
        "paper_ingestion.papers_service.delete_paper_vectors",
        new_callable=AsyncMock,
    ):
        result = await papers.bulk_action_papers.__wrapped__(
            _mock_request(),
            body=BulkActionRequest(paper_ids=[10, 20, inbox_id], action="hard_delete"),
            db_pool=pool,
        )

    assert set(result["succeeded"]) == {10, 20}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["paper_id"] == inbox_id
    # Error must be a safe enum code (no raw DB/exception text)
    assert result["failed"][0]["error"] == "conflict"


# ---------------------------------------------------------------------------
# DOM-A-07: bulk_action error sanitization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc, expected_code",
    [
        (asyncpg.UniqueViolationError(), "already_in_state"),
        (asyncpg.ForeignKeyViolationError(), "not_found"),
        (asyncpg.NotNullViolationError(), "constraint_error"),
        (asyncpg.CheckViolationError(), "constraint_error"),
        (asyncpg.PostgresError(), "db_error"),
        (ValueError("unknown bulk action: bad"), "invalid_action"),
        (HTTPException(status_code=404, detail="paper not found"), "not_found"),
        (HTTPException(status_code=403, detail="paper not owned"), "forbidden"),
        (HTTPException(status_code=409, detail="wrong state"), "conflict"),
        (RuntimeError("internal surprise"), "unknown_error"),
    ],
)
async def test_bulk_action_error_returns_safe_code(exc, expected_code):
    """DOM-A-07: bulk_action_papers must return a safe enum code, never raw exception text.

    The failed[*].error field must not contain DB schema names, constraint names,
    SQL text, or any other implementation detail.
    """
    pool = _make_pool_and_conn()[0]

    async def _always_raise(c, paper_id, user_id, action, **_kwargs):
        raise exc

    with patch.object(papers, "_apply_bulk_action", side_effect=_always_raise):
        result = await papers.bulk_action_papers.__wrapped__(
            _mock_request(),
            body=BulkActionRequest(paper_ids=[42], action="save"),
            db_pool=pool,
        )

    assert result["succeeded"] == []
    assert len(result["failed"]) == 1
    failed_entry = result["failed"][0]
    assert failed_entry["paper_id"] == 42

    error_code = failed_entry["error"]
    assert error_code == expected_code, f"Expected safe code {expected_code!r}, got {error_code!r}"
    # Belt-and-suspenders: the returned string must never contain raw DB artifact names
    assert "papers_pkey" not in error_code
    assert "asyncpg" not in error_code


# ---------------------------------------------------------------------------
# Feed counts
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_feed_counts_basic
# Survivor: test_papers_contract.py::test_a71_get_feed_counts_reflects_user_library
# Contract A71 verifies all 10-bucket fields in the FeedCountsResponse with real DB data.


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
        "paper_ingestion.papers_service.delete_paper_vectors",
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
        "paper_ingestion.papers_service.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_delete:
        with pytest.raises(_asyncpg.PostgresError):
            await papers.hard_delete_paper.__wrapped__(
                _mock_request(),
                paper_id=51,
                db_pool=pool,
            )

    # Postgres error before Qdrant call — mock must never be reached
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
        "paper_ingestion.papers_service.delete_paper_vectors",
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


# ---------------------------------------------------------------------------
# W1.7-B: re-trash guard + state preconditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trash_paper_idempotent_when_already_trashed():
    """PUT /{id}/trash on an already-trashed paper returns 200 (idempotent).

    The CASE expression in the ON CONFLICT clause must preserve state_before_trash
    rather than writing 'trash', which would violate the CHECK constraint.
    Because _trash_paper only calls conn.execute (no SELECT), the test
    just verifies that no exception is raised and the correct SQL is issued.
    """
    pool, conn = _make_pool_and_conn()
    # assert_paper_ownership + paper existence check both use fetchrow
    conn.fetchrow.return_value = {"id": 99}

    result = await papers.trash_paper.__wrapped__(
        _mock_request(),
        paper_id=99,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 99}
    # Verify the CASE expression is present in the SQL that was executed
    all_sql = [call.args[0] for call in conn.execute.await_args_list]
    trash_sql = [s for s in all_sql if "state_before_trash" in s]
    assert trash_sql, "Expected an INSERT … ON CONFLICT … state_before_trash SQL"
    assert any("CASE" in s for s in trash_sql), (
        "ON CONFLICT clause must use CASE to preserve existing state_before_trash"
    )


@pytest.mark.asyncio
async def test_trash_and_reject_succeeds_on_already_trashed_paper():
    """PUT /{id}/trash_and_reject on an already-trashed paper returns 200.

    This is the primary bug from the screenshot: a stale Pulse cache fires
    trash_and_reject on a paper that is already in 'trash', which hits the
    CHECK constraint on state_before_trash.  After the fix, the CASE expression
    in _trash_paper keeps state_before_trash intact, and the route returns 200.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 77}
    # _upsert_recommendation_feedback calls fetchval to look up topic_id
    conn.fetchval.return_value = None  # no topic association — that's fine

    result = await papers.trash_and_reject_paper.__wrapped__(
        _mock_request(),
        paper_id=77,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 77}
    # Both _trash_paper and _upsert_recommendation_feedback must have been called
    all_sql = [call.args[0] for call in conn.execute.await_args_list]
    assert any("state_before_trash" in s for s in all_sql), (
        "_trash_paper must execute its INSERT … ON CONFLICT"
    )
    assert any("recommendation_feedback" in s for s in all_sql), (
        "_upsert_recommendation_feedback must insert into recommendation_feedback"
    )


@pytest.mark.asyncio
async def test_skip_paper_rejects_when_not_in_inbox():
    """PUT /{id}/skip returns 409 when paper is in 'to_read' state.

    skip is only valid from 'inbox'; a stale-cache write from any other state
    must surface a 409 so the frontend can refetch and re-render.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 55}
    # _assert_paper_in_states fetches current state via fetchval
    conn.fetchval.return_value = "to_read"

    with pytest.raises(HTTPException) as exc_info:
        await papers.skip_paper.__wrapped__(
            _mock_request(),
            paper_id=55,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 409
    assert "inbox" in exc_info.value.detail
    assert "to_read" in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_state,fetchval_return",
    [
        ("inbox", None),  # missing row → COALESCE → 'inbox'
        ("done", "done"),
        ("to_read", "to_read"),
        ("reading", "reading"),  # W1.8-A: Set Aside (reading → to_read) must be allowed
    ],
)
async def test_save_paper_succeeds_from_inbox_done_to_read(initial_state, fetchval_return):
    """PUT /{id}/save returns 200 from inbox, done, and to_read states.

    'to_read' must stay allowed because the Pulse Save→Unsave→Save round-trip
    calls save from 'to_read' on a second Save action.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 33}
    conn.fetchval.return_value = fetchval_return  # drives _assert_paper_in_states

    result = await papers.save_paper.__wrapped__(
        _mock_request(),
        paper_id=33,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 33}, (
        f"save_paper must return 200 from '{initial_state}'"
    )


@pytest.mark.asyncio
async def test_reading_paper_rejects_from_inbox():
    """PUT /{id}/reading returns 409 when paper is in 'inbox' state.

    A paper must be saved to the reading list (to_read) before it can be
    marked as currently being read.  Stale-cache writes from inbox must 409.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 44}
    # _assert_paper_in_states: paper is in 'inbox' (COALESCE of None)
    conn.fetchval.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await papers.reading_paper.__wrapped__(
            _mock_request(),
            paper_id=44,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 409
    assert "to_read" in exc_info.value.detail or "reading" in exc_info.value.detail


# ---------------------------------------------------------------------------
# C3 Task 2.2 — vacuity-immune integration seam tests
#
# These tests drive the real DELETE /api/papers/{id} route via
# AsyncClient+ASGITransport and assert OBSERVABLE behaviour (HTTP status +
# call ordering on mocked collaborators).  They do NOT patch
# ``delete_paper_vectors`` or ``assert_paper_ownership`` — that is exactly
# what makes them immune to the C3 module move: the observable effects
# (DB delete, Qdrant delete, 403 response) are module-path-independent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seam_hard_delete_ordering_db_then_qdrant():
    """C3-SEAM-1 (WS-AH2 NEW-H2): DELETE /api/papers/{id} issues the DB DELETE
    *before* the Qdrant vector cleanup, even after papers_service extraction.

    Immunity: wires the Qdrant call via ``svc.embedder`` (the concrete
    collaborator ``delete_paper_vectors`` calls internally), not by patching
    ``delete_paper_vectors`` itself.  The assert is on the call-order list,
    which is indifferent to whether the call comes from routers.papers or
    papers_service.
    """
    from jarvis_common import verify_api_key
    from paper_ingestion._state import set_services
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()

    # Ownership check: paper discovered by user 1 (same as authed user).
    # State check: paper is in 'trash'.
    conn.fetchrow.return_value = {"discovered_by": 1}
    conn.fetchval.return_value = "trash"

    call_order: list[str] = []

    async def _recording_execute(sql, *args):  # noqa: ARG001
        if "DELETE FROM papers" in sql:
            call_order.append("db_delete")

    conn.execute.side_effect = _recording_execute

    # Wire a mock embedder — delete_paper_vectors calls svc.embedder.delete_paper_vectors.
    # We record the call here instead of patching delete_paper_vectors at any module path.
    mock_embedder = MagicMock()

    async def _recording_delete_vectors(paper_id):  # noqa: ARG001
        call_order.append("qdrant_delete")

    mock_embedder.delete_paper_vectors = AsyncMock(side_effect=_recording_delete_vectors)

    saved_embedder = set_services(embedder=mock_embedder).embedder

    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/papers/99")
    finally:
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(verify_api_key, None)
        app.state.limiter.enabled = True
        # Restore previous embedder value (None in unit-test context).
        set_services(embedder=saved_embedder)

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json() == {"deleted": 99}

    # WS-AH2 NEW-H2 ordering invariant: DB delete must precede Qdrant cleanup.
    assert call_order == ["db_delete", "qdrant_delete"], (
        f"Load-bearing ordering violated — got: {call_order}"
    )


@pytest.mark.asyncio
async def test_seam_ownership_enforced_cross_user_returns_4xx():
    """C3-SEAM-2: DELETE /api/papers/{id} returns 403 when the paper is owned
    by a different user and the caller has no user_library row.

    Immunity: does NOT patch ``assert_paper_ownership`` at any module binding.
    The assertion is on the HTTP response status, which is indifferent to
    whether the ownership check is performed in routers.papers or papers_service.

    Ownership status: 403 — confirmed at
    libs/jarvis_common/jarvis_common/db_helpers.py:318
    (``raise HTTPException(status_code=403, detail="paper not owned by current user")``).
    """
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()

    # Paper discovered_by=2 (other user); authed user is 1 (autouse fixture).
    # assert_paper_ownership checks user_library next — return None → 403.
    conn.fetchrow.return_value = {"discovered_by": 2}
    conn.fetchval.return_value = None  # not in user_library

    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/papers/77")
    finally:
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(verify_api_key, None)
        app.state.limiter.enabled = True

    # jarvis_common/db_helpers.py:318 — ownership violation → 403.
    assert resp.status_code == 403, (
        f"Expected 403 for cross-user access, got {resp.status_code}: {resp.text}"
    )
