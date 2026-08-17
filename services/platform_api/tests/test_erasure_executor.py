"""Live authority contracts for the isolated account-erasure executor."""

from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio
from jarvis_common.db_helpers import init_pg_connection

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def erasure_connections(contract_pg_dsn, _contract_pool):
    """Yield bootstrap and executor connections to the initialized contract DB."""
    password = "erasure-executor-contract-password"
    bootstrap = await asyncpg.connect(contract_pg_dsn)
    await init_pg_connection(bootstrap)
    await bootstrap.execute(
        "ALTER ROLE jarvis_erasure_executor LOGIN PASSWORD 'erasure-executor-contract-password'"
    )
    executor = await asyncpg.connect(
        contract_pg_dsn,
        user="jarvis_erasure_executor",
        password=password,
    )
    try:
        yield bootstrap, executor
    finally:
        await executor.close()
        await bootstrap.close()


async def test_executor_is_capability_only_and_duplicate_finalization_is_harmless(
    erasure_connections,
) -> None:
    """Only the executor capability finalizes, without exposing generic tables."""
    bootstrap, executor = erasure_connections
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW() - INTERVAL '31 days') RETURNING id""",
        f"executor-{uuid.uuid4().hex}@example.com",
    )
    request_id = uuid.uuid4()
    await bootstrap.execute(
        """INSERT INTO platform.erasure_requests
           (request_id, user_id, state, resume_state, eligible_at)
           VALUES ($1, $2, 'ready', 'learning_pending', NOW() - INTERVAL '1 day')""",
        request_id,
        user_id,
    )
    await bootstrap.executemany(
        """INSERT INTO platform.erasure_acknowledgements
           (request_id, domain, receipt) VALUES ($1, $2, $3::jsonb)""",
        [
            (request_id, "qdrant", {"residual_points": 0}),
            (request_id, "research", {"acknowledged": True}),
            (request_id, "learning", {"acknowledged": True}),
        ],
    )
    job_id = str(uuid.uuid4())
    await bootstrap.execute(
        """INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
           VALUES ('paper_ingestion', 'paper.process', $1::jsonb, 'succeeded')""",
        {"job_id": job_id, "user_id": user_id, "paper_id": 1},
    )

    with pytest.raises(Exception, match="executor-only"):
        await bootstrap.fetchval("SELECT platform.finalize_erasure($1)", request_id)

    first_finalize = await executor.fetchval("SELECT platform.finalize_erasure($1)", request_id)
    second_finalize = await executor.fetchval("SELECT platform.finalize_erasure($1)", request_id)
    assert first_finalize is True
    assert second_finalize is False
    assert (
        await bootstrap.fetchval("SELECT COUNT(*) FROM platform.users WHERE id = $1", user_id) == 0
    )
    assert (
        await bootstrap.fetchval(
            "SELECT state FROM platform.erasure_requests WHERE request_id = $1",
            request_id,
        )
        == "complete"
    )
    assert (
        await bootstrap.fetchval(
            "SELECT args ? 'user_id' FROM ops.procrastinate_jobs WHERE args->>'job_id' = $1",
            job_id,
        )
        is False
    )

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await executor.fetchval("SELECT COUNT(*) FROM platform.users")
