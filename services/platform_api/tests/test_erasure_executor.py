"""Live authority contracts for the isolated account-erasure executor."""

from __future__ import annotations

import uuid
from datetime import timedelta

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
    contract_pg_dsn: str,
    erasure_connections,
) -> None:
    """The executor lists and finalizes only through its exact capabilities."""
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
    ineligible_user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW() - INTERVAL '31 days') RETURNING id""",
        f"executor-ineligible-{uuid.uuid4().hex}@example.com",
    )
    ineligible_request_id = uuid.uuid4()
    await bootstrap.execute(
        """INSERT INTO platform.erasure_requests
           (request_id, user_id, state, resume_state, eligible_at)
           VALUES ($1, $2, 'ready', 'learning_pending', NOW() + INTERVAL '1 day')""",
        ineligible_request_id,
        ineligible_user_id,
    )
    job_id = str(uuid.uuid4())
    await bootstrap.execute(
        """INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
           VALUES ('paper_ingestion', 'paper.process', $1::jsonb, 'succeeded')""",
        {"job_id": job_id, "user_id": user_id, "paper_id": 1},
    )

    with pytest.raises(Exception, match="executor-only"):
        await bootstrap.fetchval("SELECT platform.finalize_erasure($1)", request_id)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await executor.fetch("SELECT request_id FROM platform.erasure_requests")
    for limit in (None, -1, 0, 101):
        with pytest.raises(asyncpg.RaiseError, match="due-request listing is not allowed"):
            await executor.fetch(
                "SELECT request_id FROM platform.due_erasure_request_ids($1)", limit
            )

    due_ids = await executor.fetch(
        "SELECT request_id FROM platform.due_erasure_request_ids($1)", 20
    )
    assert [row["request_id"] for row in due_ids] == [request_id]
    assert ineligible_request_id not in [row["request_id"] for row in due_ids]

    pool = await asyncpg.create_pool(
        contract_pg_dsn,
        user="jarvis_erasure_executor",
        password="erasure-executor-contract-password",
        min_size=1,
        max_size=1,
    )
    try:
        from platform_api.erasure_executor import finalize_due_requests

        assert await finalize_due_requests(pool, limit=20) == 1
    finally:
        await pool.close()

    repeat_finalize = await executor.fetchval("SELECT platform.finalize_erasure($1)", request_id)
    assert repeat_finalize is False
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

    await bootstrap.execute(
        "ALTER ROLE jarvis_research_runtime LOGIN PASSWORD 'research-runtime-contract-password'"
    )
    research = await asyncpg.connect(
        contract_pg_dsn,
        user="jarvis_research_runtime",
        password="research-runtime-contract-password",
    )
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await research.fetch("SELECT request_id FROM platform.due_erasure_request_ids($1)", 1)
    finally:
        await research.close()


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def platform_runtime_connection(contract_pg_dsn, _contract_pool):
    """Yield a connection authenticated as the platform runtime role."""
    password = "platform-runtime-contract-password"
    bootstrap = await asyncpg.connect(contract_pg_dsn)
    await init_pg_connection(bootstrap)
    await bootstrap.execute(
        "ALTER ROLE jarvis_platform_runtime LOGIN PASSWORD 'platform-runtime-contract-password'"
    )
    runtime = await asyncpg.connect(
        contract_pg_dsn,
        user="jarvis_platform_runtime",
        password=password,
    )
    try:
        yield bootstrap, runtime
    finally:
        await runtime.close()
        await bootstrap.close()


async def test_platform_runtime_cannot_bypass_the_erasure_invariants(
    platform_runtime_connection,
) -> None:
    """Erasure state and the deletion clock are reachable only as capabilities.

    Direct writes would let the service that requests an erasure also assemble
    its preconditions: back-date the grace window, forge the acknowledgements
    finalization counts, or remove the account outright.
    """
    bootstrap, runtime = platform_runtime_connection
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW()) RETURNING id""",
        f"runtime-{uuid.uuid4().hex}@example.com",
    )

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await runtime.execute(
            """INSERT INTO platform.erasure_requests
               (request_id, user_id, state, resume_state, eligible_at)
               VALUES ($1, $2, 'ready', 'learning_pending', NOW())""",
            uuid.uuid4(),
            user_id,
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await runtime.execute(
            """INSERT INTO platform.erasure_acknowledgements (request_id, domain, receipt)
               VALUES ($1, 'research', '{}'::jsonb)""",
            uuid.uuid4(),
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await runtime.execute(
            "UPDATE platform.users SET deleted_at = NOW() - INTERVAL '99 days' WHERE id = $1",
            user_id,
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await runtime.execute("DELETE FROM platform.users WHERE id = $1", user_id)

    # Account administration the runtime legitimately owns still works.
    await runtime.execute(
        "UPDATE platform.users SET display_name = 'renamed' WHERE id = $1", user_id
    )

    # The capability derives the grace window itself, so a request created now
    # is not yet due and the destructive phase refuses to start.
    request_id = await runtime.fetchval("SELECT platform.request_erasure_v1($1)", user_id)
    assert request_id is not None
    with pytest.raises(asyncpg.RaiseError, match="restore grace"):
        await runtime.fetch("SELECT * FROM platform.begin_erasure_destructive_v1($1)", request_id)

    # Completion stays reserved for the executor's finalization capability.
    with pytest.raises(asyncpg.RaiseError, match="reserved for finalization"):
        await runtime.fetchval(
            "SELECT platform.transition_erasure_v1($1, 'complete', 'requested')", request_id
        )

    eligible_at = await bootstrap.fetchval(
        "SELECT eligible_at FROM platform.erasure_requests WHERE request_id = $1", request_id
    )
    deleted_at = await bootstrap.fetchval(
        "SELECT deleted_at FROM platform.users WHERE id = $1", user_id
    )
    assert eligible_at == deleted_at + timedelta(days=30)
