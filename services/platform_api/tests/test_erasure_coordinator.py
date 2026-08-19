"""Live contracts for the durable Platform erasure state machine."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from jarvis_common.testing import SharedConnPool
from platform_api.config import PlatformSettings
from platform_api.repos import erasure
from platform_api.services.erasure import (
    ErasureNotEligibleError,
    process_due_requests,
    process_request,
    start_coordinator,
    stop_coordinator,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_request(conn, *, deleted_days: int, state: str = "requested") -> uuid.UUID:
    """Create one deleted user and matching erasure request for a live contract."""
    user_id = await conn.fetchval(
        "INSERT INTO platform.users (email, role, deleted_at) "
        "VALUES ($1, 'user', NOW() - $2::interval) RETURNING id",
        f"erasure-{uuid.uuid4().hex}@example.com",
        timedelta(days=deleted_days),
    )
    request_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO platform.erasure_requests
           (request_id, user_id, state, resume_state, eligible_at)
           VALUES ($1, $2, $3, 'qdrant_pending',
                   (SELECT deleted_at + INTERVAL '30 days'
                    FROM platform.users WHERE id = $2))""",
        request_id,
        user_id,
        state,
    )
    return request_id


async def test_due_requests_exclude_the_restore_window(contract_conn) -> None:
    """Only accounts whose full restore grace elapsed are coordinator work."""
    recent = await _seed_request(contract_conn, deleted_days=29)
    due = await _seed_request(contract_conn, deleted_days=31)

    request_ids = await erasure.due_request_ids(SharedConnPool(contract_conn), limit=20)

    assert due in request_ids
    assert recent not in request_ids


_SEED_PHASES: dict[str, tuple[str, ...]] = {
    "requested": (),
    "qdrant_pending": (),
    "research_pending": ("research_pending",),
    "learning_pending": ("research_pending", "learning_pending"),
}


async def _seed_request_through_capabilities(
    conn, *, deleted_days: int, state: str = "requested"
) -> uuid.UUID:
    """Create one request on the runtime connection using only its capabilities.

    The runtime cannot write the erasure tables directly, so a contract that
    exercises it has to reach the wanted phase the way production does. Seeding
    and exercising share one connection because each holds its own transaction.
    """
    user_id = await conn.fetchval(
        "INSERT INTO platform.users (email, role, deleted_at) "
        "VALUES ($1, 'user', NOW() - $2::interval) RETURNING id",
        f"erasure-{uuid.uuid4().hex}@example.com",
        timedelta(days=deleted_days),
    )
    request_id = await conn.fetchval("SELECT platform.request_erasure_v1($1)", user_id)
    if state == "requested":
        return request_id
    await conn.fetch("SELECT * FROM platform.begin_erasure_destructive_v1($1)", request_id)
    current = "qdrant_pending"
    for target in _SEED_PHASES[state]:
        await conn.fetchval(
            "SELECT platform.transition_erasure_v1($1, $2, $3)", request_id, target, current
        )
        current = target
    return request_id


async def test_transition_rejects_a_skipped_phase(platform_runtime_conn) -> None:
    """The persisted graph cannot jump from requested directly to ready."""
    request_id = await _seed_request_through_capabilities(platform_runtime_conn, deleted_days=31)
    pool = SharedConnPool(platform_runtime_conn)

    with pytest.raises(ValueError, match="requested.*ready"):
        await erasure.transition(pool, request_id, erasure.ErasureState.READY)

    state = await erasure.transition(pool, request_id, erasure.ErasureState.QDRANT_PENDING)
    assert state is erasure.ErasureState.QDRANT_PENDING


async def test_retry_persists_and_resumes_the_exact_phase(
    platform_runtime_conn,
) -> None:
    """A temporary owner outage resumes only the incomplete durable phase."""
    request_id = await _seed_request_through_capabilities(
        platform_runtime_conn, deleted_days=31, state="learning_pending"
    )
    pool = SharedConnPool(platform_runtime_conn)

    state = await erasure.record_retry(
        pool,
        request_id,
        erasure.ErasureState.LEARNING_PENDING,
    )
    row = await platform_runtime_conn.fetchrow(
        "SELECT state, resume_state, attempts FROM platform.erasure_requests WHERE request_id = $1",
        request_id,
    )

    assert state is erasure.ErasureState.RETRY_WAIT
    assert row is not None
    assert dict(row) == {
        "state": "retry_wait",
        "resume_state": "learning_pending",
        "attempts": 1,
    }

    resumed = await erasure.transition(
        pool,
        request_id,
        erasure.ErasureState.LEARNING_PENDING,
    )
    assert resumed is erasure.ErasureState.LEARNING_PENDING


async def test_duplicate_acknowledgements_replace_one_durable_receipt(
    platform_runtime_conn,
) -> None:
    """Duplicate delivery updates one domain vote instead of adding another."""
    request_id = await _seed_request_through_capabilities(platform_runtime_conn, deleted_days=31)
    pool = SharedConnPool(platform_runtime_conn)

    await erasure.acknowledge(pool, request_id, "learning", {"attempt": 1})
    await erasure.acknowledge(pool, request_id, "learning", {"attempt": 2})

    rows = await platform_runtime_conn.fetch(
        "SELECT receipt FROM platform.erasure_acknowledgements "
        "WHERE request_id = $1 AND domain = 'learning'",
        request_id,
    )
    assert len(rows) == 1
    assert rows[0]["receipt"] == {"attempt": 2}


async def test_resume_after_learning_outage_does_not_repeat_completed_owners(
    platform_runtime_conn,
) -> None:
    """Persisted Qdrant and Research receipts narrow retry to Learning only."""
    request_id = await _seed_request_through_capabilities(
        platform_runtime_conn, deleted_days=31, state="learning_pending"
    )
    pool = SharedConnPool(platform_runtime_conn)
    await erasure.acknowledge(pool, request_id, "qdrant", {"residual_points": 0})
    await erasure.acknowledge(pool, request_id, "research", {"acknowledged": True})

    response = MagicMock()
    response.json.return_value = {"acknowledged": True}
    client = AsyncMock()
    client.post.return_value = response
    app = FastAPI()
    app.state.db_pool = pool
    app.state.http_client = client
    app.state.identity_signer = MagicMock()
    app.state.identity_signer.issue.return_value = "signed"

    state = await process_request(app, request_id, settings=PlatformSettings())

    assert state is erasure.ErasureState.READY
    client.post.assert_awaited_once()
    assert client.post.await_args.args[0].endswith(f"/internal/domains/erasure/{request_id}")


async def test_due_pass_isolates_a_concurrent_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One restored account cannot prevent later due requests from advancing."""
    restored_request = uuid.uuid4()
    due_request = uuid.uuid4()
    due_ids = AsyncMock(return_value=[restored_request, due_request])
    process = AsyncMock(
        side_effect=[
            ErasureNotEligibleError("account was restored"),
            erasure.ErasureState.READY,
        ]
    )
    monkeypatch.setattr(erasure, "due_request_ids", due_ids)
    monkeypatch.setattr("platform_api.services.erasure.process_request", process)
    app = FastAPI()
    app.state.db_pool = object()

    considered = await process_due_requests(app)

    assert considered == 2
    assert [call.args[1] for call in process.await_args_list] == [restored_request, due_request]


async def test_restore_lock_cancels_a_waiting_destructive_start(
    contract_pg_dsn: str,
    _contract_pool,
    platform_runtime_conn,
) -> None:
    """A restore holding the account rows wins before destructive work begins."""
    restore_conn = await asyncpg.connect(contract_pg_dsn)
    pool = SharedConnPool(platform_runtime_conn)
    transaction = restore_conn.transaction()
    committed = False
    try:
        request_id = await _seed_request(restore_conn, deleted_days=31)
        user_id = await restore_conn.fetchval(
            "SELECT user_id FROM platform.erasure_requests WHERE request_id = $1",
            request_id,
        )
        await transaction.start()
        await restore_conn.fetchrow(
            "SELECT request_id FROM platform.erasure_requests WHERE request_id = $1 FOR UPDATE",
            request_id,
        )
        await restore_conn.fetchrow(
            "SELECT id FROM platform.users WHERE id = $1 FOR UPDATE",
            user_id,
        )

        destructive_start = asyncio.create_task(erasure.begin_destructive_phases(pool, request_id))
        await asyncio.sleep(0.05)
        assert not destructive_start.done()

        await restore_conn.execute(
            "UPDATE platform.users SET deleted_at = NULL WHERE id = $1",
            user_id,
        )
        await restore_conn.execute(
            "DELETE FROM platform.erasure_requests WHERE request_id = $1",
            request_id,
        )
        await transaction.commit()
        committed = True

        with pytest.raises(LookupError, match="does not exist"):
            await destructive_start
    finally:
        if not committed and restore_conn.is_in_transaction():
            await transaction.rollback()
        await restore_conn.close()


async def test_platform_lifespan_pairs_erasure_coordinator_hooks() -> None:
    """Platform starts and stops the durable coordinator with application life."""
    from platform_api.main import _lifespan_config

    index = _lifespan_config.custom_init_tasks.index(start_coordinator)

    assert _lifespan_config.custom_teardown_tasks[index] is stop_coordinator
