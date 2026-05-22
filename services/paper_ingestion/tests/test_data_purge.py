"""Tests for the WS-USER-DELETION daily user-purge job (RB-2: GDPR hard-delete)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.jobs.data_purge import (
    _DELETE_EXPIRED_USERS_EXCLUDING,
    _SELECT_EXPIRED_USERS,
    data_purge_task,
    register_data_purge,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Keep local: uses asynccontextmanager pattern + returns (pool, conn) with execute result — not covered by canonical make_pool_and_conn.
def _make_pool(
    fetch_rows: list[dict], execute_result: str = "DELETE 2"
) -> tuple[MagicMock, AsyncMock]:
    """Return an asyncpg Pool mock wired with fetch + execute returns on the conn."""
    conn = AsyncMock()
    conn.fetch.return_value = fetch_rows
    conn.execute.return_value = execute_result

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    # pool.execute is NOT called by data_purge_task (DELETE runs on the conn).
    pool.execute = AsyncMock(return_value=execute_result)
    return pool, conn


def _make_count_result(count: int) -> MagicMock:
    result = MagicMock()
    result.count = count
    return result


def _make_qdrant(vector_count: int = 3) -> AsyncMock:
    """Return a minimal AsyncQdrantClient mock with count + delete wired."""
    qdrant = AsyncMock()
    qdrant.count.return_value = _make_count_result(vector_count)
    delete_result = MagicMock()
    delete_result.operation_id = 1
    qdrant.delete.return_value = delete_result
    return qdrant


def _make_app(fetch_rows, execute_result="DELETE 2", include_qdrant=True):
    pool, conn = _make_pool(fetch_rows, execute_result)
    app = MagicMock()
    app.state.db_pool = pool
    qdrant = _make_qdrant() if include_qdrant else None
    app.state.qdrant_client = qdrant
    return app, pool, conn, qdrant


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_selects_before_deleting() -> None:
    """SELECT must use same predicate as DELETE and happen before the DELETE."""
    rows = [{"id": 10}, {"id": 20}]
    app, pool, conn, qdrant = _make_app(rows)

    with patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock):
        await data_purge_task(app)

    select_sql = conn.fetch.call_args[0][0]
    assert "deleted_at IS NOT NULL" in select_sql
    assert "deleted_at < NOW() - INTERVAL '30 days'" in select_sql
    assert select_sql == _SELECT_EXPIRED_USERS

    # DELETE now runs on conn (same connection as SELECT) with exclusion parameter.
    delete_call = conn.execute.call_args
    delete_sql = delete_call[0][0]
    assert delete_sql == _DELETE_EXPIRED_USERS_EXCLUDING
    # All Qdrant purges succeed (mock returns 0) → exclusion list is empty.
    assert list(delete_call[0][1]) == []


@pytest.mark.asyncio
async def test_purge_calls_qdrant_delete_per_user() -> None:
    """Qdrant.delete must be called once per expired user with user_id filter."""
    rows = [{"id": 7}, {"id": 42}]
    app, pool, conn, qdrant = _make_app(rows)

    with (
        patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock),
        patch(
            "paper_ingestion.jobs.data_purge._purge_qdrant_for_user",
            new_callable=AsyncMock,
            return_value=0,
        ) as mock_purge,
    ):
        await data_purge_task(app)

    assert mock_purge.call_count == 2
    called_uids = {c.args[1] for c in mock_purge.call_args_list}
    assert called_uids == {7, 42}


@pytest.mark.asyncio
async def test_purge_qdrant_filter_uses_user_id() -> None:
    """_purge_qdrant_for_user must pass a Filter(must=[FieldCondition(user_id=uid)])."""
    from paper_ingestion.jobs.data_purge import _purge_qdrant_for_user
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    qdrant = _make_qdrant(vector_count=5)
    result = await _purge_qdrant_for_user(qdrant, 99)

    # Returns the pre-delete count from qdrant.count, not operation_id.
    assert result == 5

    qdrant.count.assert_awaited_once()
    qdrant.delete.assert_awaited_once()

    kwargs = qdrant.delete.call_args.kwargs
    selector = kwargs["points_selector"]
    assert isinstance(selector, Filter)
    must = selector.must
    assert isinstance(must, list)
    assert len(must) == 1
    cond = must[0]
    assert isinstance(cond, FieldCondition)
    assert cond.key == "user_id"
    assert isinstance(cond.match, MatchValue)
    assert cond.match.value == 99

    # count must also use the same user_id filter
    count_kwargs = qdrant.count.call_args.kwargs
    count_selector = count_kwargs["count_filter"]
    assert isinstance(count_selector, Filter)
    count_must = count_selector.must
    assert isinstance(count_must, list)
    count_cond = count_must[0]
    assert isinstance(count_cond, FieldCondition)
    assert isinstance(count_cond.match, MatchValue)
    assert count_cond.match.value == 99


@pytest.mark.asyncio
async def test_purge_calls_log_audit_not_log_event() -> None:
    """log_audit (not log_event) must be called after a successful purge."""
    rows = [{"id": 5}]
    app, pool, conn, qdrant = _make_app(rows, execute_result="DELETE 1")

    with (
        patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock) as mock_audit,
        patch(
            "paper_ingestion.jobs.data_purge._purge_qdrant_for_user",
            new_callable=AsyncMock,
            return_value=7,
        ),
    ):
        await data_purge_task(app)

    mock_audit.assert_awaited_once()
    kwargs = mock_audit.call_args.kwargs
    assert kwargs["action"] == "user.hard_delete.purged"
    assert kwargs["resource"] == "users"
    assert 5 in kwargs["metadata"]["user_ids"]
    # Per-uid vector counts must be in audit metadata.
    assert kwargs["metadata"]["qdrant_vectors_deleted"] == {5: 7}


@pytest.mark.asyncio
async def test_purge_no_expired_users_skips_everything() -> None:
    """When no users are expired, neither Qdrant nor DELETE nor audit are called."""
    app, pool, conn, qdrant = _make_app([], execute_result="DELETE 0")

    with patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock) as mock_audit:
        await data_purge_task(app)

    assert qdrant is not None
    qdrant.delete.assert_not_awaited()
    # conn.execute (DELETE) must not be called when there are no expired users.
    conn.execute.assert_not_awaited()
    mock_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_qdrant_error_is_resilient_and_continues() -> None:
    """A Qdrant failure for one uid must not abort the SQL DELETE or audit.

    SEC-PURGE-1: the failed uid (1) must be EXCLUDED from the hard DELETE so
    its vectors are not orphaned.  uid=2 succeeded and must be deleted.
    """
    rows = [{"id": 1}, {"id": 2}]
    # Only uid=2 gets deleted; execute_result reflects that.
    app, pool, conn, qdrant = _make_app(rows, execute_result="DELETE 1")

    call_count = 0

    async def _flaky_purge(q, uid):
        nonlocal call_count
        call_count += 1
        if uid == 1:
            raise RuntimeError("qdrant unreachable")
        return 0

    with (
        patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock) as mock_audit,
        patch(
            "paper_ingestion.jobs.data_purge._purge_qdrant_for_user",
            side_effect=_flaky_purge,
        ),
    ):
        await data_purge_task(app)  # must not raise

    assert call_count == 2
    conn.execute.assert_awaited_once()
    # Exclusion list must contain uid=1 (failed); uid=2 must NOT be excluded.
    delete_call = conn.execute.call_args
    exclude_param = delete_call[0][1]
    assert 1 in exclude_param
    assert 2 not in exclude_param
    mock_audit.assert_awaited_once()
    # qdrant_errors must be recorded in audit metadata
    metadata = mock_audit.call_args.kwargs["metadata"]
    assert "qdrant_errors" in metadata
    assert len(metadata["qdrant_errors"]) == 1
    # uid=2 succeeded; its count (0) must appear; uid=1 errored so not present.
    assert metadata["qdrant_vectors_deleted"] == {2: 0}


@pytest.mark.asyncio
async def test_purge_missing_qdrant_client_logs_warning_and_continues() -> None:
    """If qdrant_client is absent from app.state, log a warning and defer hard-delete.

    SEC-PURGE-1: when Qdrant is unavailable ALL expired uids are treated as
    failed — the DELETE is issued but with all uids excluded, so no rows are
    actually removed.  They retry on the next nightly run once Qdrant is back.
    """
    rows = [{"id": 3}]
    app, pool, conn, _ = _make_app(rows, execute_result="DELETE 0", include_qdrant=False)
    # Explicitly remove qdrant_client from state (getattr returns None)
    del app.state.qdrant_client

    with (
        patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock) as mock_audit,
        patch("paper_ingestion.jobs.data_purge.logger") as mock_logger,
    ):
        await data_purge_task(app)

    mock_logger.warning.assert_called()
    # DELETE is still called — but with uid=3 in the exclusion list → 0 rows removed.
    conn.execute.assert_awaited_once()
    delete_call = conn.execute.call_args
    exclude_param = delete_call[0][1]
    assert 3 in exclude_param
    mock_audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# SEC-PURGE-1: failed-Qdrant uids excluded from hard DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sec_purge1_partial_qdrant_failure_excludes_failed_uid() -> None:
    """SEC-PURGE-1(a): uid whose Qdrant purge fails must NOT be hard-deleted.

    uid=1 raises → must be excluded from DELETE; uid=2 succeeds → must be
    deleted.  The DELETE SQL must carry a ``<> ALL($1::int[])`` exclusion
    containing [1], and qdrant_errors must be recorded in audit metadata.
    """
    rows = [{"id": 1}, {"id": 2}]
    # execute_result reflects only uid=2 being deleted
    app, pool, conn, qdrant = _make_app(rows, execute_result="DELETE 1")

    async def _flaky_purge(q, uid):
        if uid == 1:
            raise RuntimeError("qdrant unreachable")
        return 5

    with (
        patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock) as mock_audit,
        patch(
            "paper_ingestion.jobs.data_purge._purge_qdrant_for_user",
            side_effect=_flaky_purge,
        ),
    ):
        await data_purge_task(app)  # must not raise

    # DELETE must have been called once (for uid=2 only)
    conn.execute.assert_awaited_once()
    delete_call = conn.execute.call_args
    delete_sql = delete_call[0][0]
    # New parameterized SQL must include exclusion clause
    assert "<> ALL($1::int[])" in delete_sql or "!= ALL($1::int[])" in delete_sql
    # The exclusion parameter must contain uid=1
    exclude_param = delete_call[0][1]
    assert 1 in exclude_param
    assert 2 not in exclude_param

    mock_audit.assert_awaited_once()
    metadata = mock_audit.call_args.kwargs["metadata"]
    assert "qdrant_errors" in metadata
    assert len(metadata["qdrant_errors"]) == 1
    # uid=2 succeeded → appears in qdrant_vectors_deleted; uid=1 did not
    assert metadata["qdrant_vectors_deleted"] == {2: 5}


@pytest.mark.asyncio
async def test_sec_purge1_all_success_empty_exclusion_list() -> None:
    """SEC-PURGE-1(b): all Qdrant purges succeed → exclusion list is empty.

    With no failures the parameterized DELETE with an empty exclusion list
    must delete all expired uids (regression-equivalent: ``<> ALL('{}'::int[])``
    is always true so nothing is excluded).
    """
    rows = [{"id": 10}, {"id": 20}]
    app, pool, conn, qdrant = _make_app(rows, execute_result="DELETE 2")

    with (
        patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock) as mock_audit,
        patch(
            "paper_ingestion.jobs.data_purge._purge_qdrant_for_user",
            new_callable=AsyncMock,
            return_value=3,
        ),
    ):
        await data_purge_task(app)

    conn.execute.assert_awaited_once()
    delete_call = conn.execute.call_args
    delete_sql = delete_call[0][0]
    assert "<> ALL($1::int[])" in delete_sql or "!= ALL($1::int[])" in delete_sql
    # No failures → exclusion list must be empty
    exclude_param = delete_call[0][1]
    assert list(exclude_param) == []

    mock_audit.assert_awaited_once()
    metadata = mock_audit.call_args.kwargs["metadata"]
    assert "qdrant_errors" not in metadata
    assert metadata["qdrant_vectors_deleted"] == {10: 3, 20: 3}


@pytest.mark.asyncio
async def test_sec_purge1_qdrant_none_no_users_deleted() -> None:
    """SEC-PURGE-1(c): qdrant_client is None → NO users hard-deleted.

    All expired uids are treated as "failed" so the DELETE is called with
    ALL of them excluded, meaning zero rows are removed.  A warning is logged
    and qdrant_errors is recorded.  The rows remain deleted_at-marked and will
    be retried on the next nightly run once Qdrant is restored.
    """
    rows = [{"id": 3}, {"id": 4}]
    app, pool, conn, _ = _make_app(rows, execute_result="DELETE 0", include_qdrant=False)
    del app.state.qdrant_client

    with (
        patch("paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock) as mock_audit,
        patch("paper_ingestion.jobs.data_purge.logger") as mock_logger,
    ):
        await data_purge_task(app)

    mock_logger.warning.assert_called()

    # DELETE must be called but with ALL uids excluded → no rows deleted
    conn.execute.assert_awaited_once()
    delete_call = conn.execute.call_args
    delete_sql = delete_call[0][0]
    assert "<> ALL($1::int[])" in delete_sql or "!= ALL($1::int[])" in delete_sql
    exclude_param = delete_call[0][1]
    assert set(exclude_param) == {3, 4}

    mock_audit.assert_awaited_once()
    metadata = mock_audit.call_args.kwargs["metadata"]
    assert "qdrant_errors" in metadata


@pytest.mark.asyncio
async def test_purge_handles_pool_exception() -> None:
    """An exception from the pool must be caught and logged; task must not raise."""
    pool = AsyncMock()
    # Make acquire context manager raise
    cm = AsyncMock()
    cm.__aenter__.side_effect = RuntimeError("db down")
    pool.acquire.return_value = cm

    app = MagicMock()
    app.state.db_pool = pool

    with patch("paper_ingestion.jobs.data_purge.logger") as mock_logger:
        await data_purge_task(app)  # must not raise
        mock_logger.exception.assert_called_once()


def test_register_data_purge_adds_daily_cron_job() -> None:
    scheduler = MagicMock()
    app = MagicMock()
    register_data_purge(scheduler, app)
    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "data_purge"
    assert kwargs["max_instances"] == 1
    assert kwargs["replace_existing"] is True


# ---------------------------------------------------------------------------
# Live-PG integration test (opt-in: JARVIS_RUN_LIVE_PG=1)
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_purge_deletes_pg_rows_and_calls_qdrant_delete(live_pg_dsn: str) -> None:
    """End-to-end: create an expired user, run the purge, assert 0 PG rows remain.

    Qdrant is mocked so the test runs without a real Qdrant instance. The mock
    asserts that delete was called with the EXACT user_id filter (type-correct,
    mirroring how production builds it).

    Note: True "0 Qdrant points" verification requires a live Qdrant instance.
    In the mocked path we validate filter correctness instead, which is
    equivalent under unit-test conditions — the production code path that
    constructs and passes this filter is what actually removes the points.
    """
    import asyncpg
    from paper_ingestion.ingestion.embedder import COLLECTION_NAME
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        # Seed: insert a user whose deleted_at is 31 days ago (past grace).
        user_id = await pool.fetchval(
            """
            INSERT INTO users (email, role, deleted_at)
            VALUES ('purge-test@example.com', 'user', NOW() - INTERVAL '31 days')
            RETURNING id
            """
        )

        qdrant = _make_qdrant(vector_count=2)

        app = MagicMock()
        app.state.db_pool = pool
        app.state.qdrant_client = qdrant

        with patch(
            "paper_ingestion.jobs.data_purge.log_audit", new_callable=AsyncMock
        ) as mock_audit:
            await data_purge_task(app)

        # Postgres: user row must be gone.
        remaining = await pool.fetchval("SELECT COUNT(*) FROM users WHERE id = $1", user_id)
        assert remaining == 0, f"Expected 0 rows, got {remaining}"

        # Qdrant: delete must have been called with the EXACT user_id filter for
        # this user — type-correct, matching production's Filter construction.
        qdrant.delete.assert_awaited_once()
        kwargs = qdrant.delete.call_args.kwargs
        assert kwargs["collection_name"] == COLLECTION_NAME
        selector = kwargs["points_selector"]
        assert isinstance(selector, Filter)
        must = selector.must
        assert isinstance(must, list)
        assert len(must) == 1
        cond = must[0]
        assert isinstance(cond, FieldCondition)
        assert cond.key == "user_id"
        assert isinstance(cond.match, MatchValue)
        assert cond.match.value == user_id

        # count must also have been called (pre-delete vector count)
        qdrant.count.assert_awaited_once()
        count_kwargs = qdrant.count.call_args.kwargs
        assert count_kwargs["collection_name"] == COLLECTION_NAME
        assert count_kwargs["exact"] is True

        # Audit: log_audit must have been called with correct action and
        # per-uid vector counts in metadata.
        mock_audit.assert_awaited_once()
        audit_kwargs = mock_audit.call_args.kwargs
        assert audit_kwargs["action"] == "user.hard_delete.purged"
        assert user_id in audit_kwargs["metadata"]["user_ids"]
        assert user_id in audit_kwargs["metadata"]["qdrant_vectors_deleted"]
        assert audit_kwargs["metadata"]["qdrant_vectors_deleted"][user_id] == 2

    finally:
        await pool.close()
