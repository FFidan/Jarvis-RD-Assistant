"""Tests for the WS-USER-DELETION daily user-purge job (RB-2: GDPR hard-delete)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.jobs.data_purge import (
    _DELETE_EXPIRED_USERS,
    _SELECT_EXPIRED_USERS,
    data_purge_task,
    register_data_purge,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(
    fetch_rows: list[dict], execute_result: str = "DELETE 2"
) -> tuple[MagicMock, AsyncMock]:
    """Return an asyncpg Pool mock wired with fetch + execute returns."""
    conn = AsyncMock()
    conn.fetch.return_value = fetch_rows
    conn.execute.return_value = execute_result

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool.execute = AsyncMock(return_value=execute_result)
    return pool, conn


def _make_qdrant() -> AsyncMock:
    """Return a minimal AsyncQdrantClient mock."""
    qdrant = AsyncMock()
    result = MagicMock()
    result.operation_id = 1
    qdrant.delete.return_value = result
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

    with patch("jarvis_common.audit.log_audit", new_callable=AsyncMock):
        await data_purge_task(app)

    select_sql = conn.fetch.call_args[0][0]
    assert "deleted_at IS NOT NULL" in select_sql
    assert "deleted_at < NOW() - INTERVAL '30 days'" in select_sql
    assert select_sql == _SELECT_EXPIRED_USERS

    delete_sql = pool.execute.call_args[0][0]
    assert delete_sql == _DELETE_EXPIRED_USERS


@pytest.mark.asyncio
async def test_purge_calls_qdrant_delete_per_user() -> None:
    """Qdrant.delete must be called once per expired user with user_id filter."""
    rows = [{"id": 7}, {"id": 42}]
    app, pool, conn, qdrant = _make_app(rows)

    with (
        patch("jarvis_common.audit.log_audit", new_callable=AsyncMock),
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

    qdrant = _make_qdrant()
    await _purge_qdrant_for_user(qdrant, 99)

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


@pytest.mark.asyncio
async def test_purge_calls_log_audit_not_log_event() -> None:
    """log_audit (not log_event) must be called after a successful purge."""
    rows = [{"id": 5}]
    app, pool, conn, qdrant = _make_app(rows, execute_result="DELETE 1")

    with (
        patch("jarvis_common.audit.log_audit", new_callable=AsyncMock) as mock_audit,
        patch(
            "paper_ingestion.jobs.data_purge._purge_qdrant_for_user",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        await data_purge_task(app)

    mock_audit.assert_awaited_once()
    kwargs = mock_audit.call_args.kwargs
    assert kwargs["action"] == "user.hard_delete.purged"
    assert kwargs["resource"] == "users"
    assert 5 in kwargs["metadata"]["user_ids"]


@pytest.mark.asyncio
async def test_purge_no_expired_users_skips_everything() -> None:
    """When no users are expired, neither Qdrant nor DELETE nor audit are called."""
    app, pool, conn, qdrant = _make_app([], execute_result="DELETE 0")

    with patch("jarvis_common.audit.log_audit", new_callable=AsyncMock) as mock_audit:
        await data_purge_task(app)

    assert qdrant is not None
    qdrant.delete.assert_not_awaited()
    pool.execute.assert_not_awaited()
    mock_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_qdrant_error_is_resilient_and_continues() -> None:
    """A Qdrant failure for one uid must not abort the SQL DELETE or audit."""
    rows = [{"id": 1}, {"id": 2}]
    app, pool, conn, qdrant = _make_app(rows, execute_result="DELETE 2")

    call_count = 0

    async def _flaky_purge(q, uid):
        nonlocal call_count
        call_count += 1
        if uid == 1:
            raise RuntimeError("qdrant unreachable")
        return 0

    with (
        patch("jarvis_common.audit.log_audit", new_callable=AsyncMock) as mock_audit,
        patch(
            "paper_ingestion.jobs.data_purge._purge_qdrant_for_user",
            side_effect=_flaky_purge,
        ),
    ):
        await data_purge_task(app)  # must not raise

    assert call_count == 2
    pool.execute.assert_awaited_once()
    mock_audit.assert_awaited_once()
    # qdrant_errors must be recorded in audit metadata
    metadata = mock_audit.call_args.kwargs["metadata"]
    assert "qdrant_errors" in metadata
    assert len(metadata["qdrant_errors"]) == 1


@pytest.mark.asyncio
async def test_purge_missing_qdrant_client_logs_warning_and_continues() -> None:
    """If qdrant_client is absent from app.state, log a warning but still DELETE."""
    rows = [{"id": 3}]
    app, pool, conn, _ = _make_app(rows, execute_result="DELETE 1", include_qdrant=False)
    # Explicitly remove qdrant_client from state (getattr returns None)
    del app.state.qdrant_client

    with (
        patch("jarvis_common.audit.log_audit", new_callable=AsyncMock) as mock_audit,
        patch("paper_ingestion.jobs.data_purge.logger") as mock_logger,
    ):
        await data_purge_task(app)

    mock_logger.warning.assert_called()
    pool.execute.assert_awaited_once()
    mock_audit.assert_awaited_once()


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
    asserts that delete was called with the correct user_id filter.
    """
    import asyncpg
    from paper_ingestion.ingestion.embedder import COLLECTION_NAME

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

        qdrant = _make_qdrant()

        app = MagicMock()
        app.state.db_pool = pool
        app.state.qdrant_client = qdrant

        with patch("jarvis_common.audit.log_audit", new_callable=AsyncMock) as mock_audit:
            await data_purge_task(app)

        # Postgres: user row must be gone.
        remaining = await pool.fetchval("SELECT COUNT(*) FROM users WHERE id = $1", user_id)
        assert remaining == 0, f"Expected 0 rows, got {remaining}"

        # Qdrant: delete must have been called with user_id filter.
        qdrant.delete.assert_awaited_once()
        kwargs = qdrant.delete.call_args.kwargs
        assert kwargs["collection_name"] == COLLECTION_NAME
        selector = kwargs["points_selector"]
        cond = selector.must[0]
        assert cond.key == "user_id"
        assert cond.match.value == user_id

        # Audit: log_audit must have been called with correct action.
        mock_audit.assert_awaited_once()
        assert mock_audit.call_args.kwargs["action"] == "user.hard_delete.purged"
        assert user_id in mock_audit.call_args.kwargs["metadata"]["user_ids"]

    finally:
        await pool.close()
