"""Live authority contracts for the isolated account-erasure executor."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from jarvis_common.db_helpers import init_pg_connection

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]


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


async def test_one_unfinishable_request_does_not_block_the_next(
    contract_pg_dsn: str,
    erasure_connections,
) -> None:
    """A request the finalizer refuses cannot starve the requests behind it.

    The due list is ordered by eligibility, so the oldest unfinishable request
    is selected first on every pass; without isolation it would fail the pass
    before the healthy request behind it was ever attempted, and the container
    would restart into the same failure indefinitely.
    """
    bootstrap, executor = erasure_connections
    unfinishable_id = uuid.uuid4()
    healthy_id = uuid.uuid4()
    for request_id, offset in ((unfinishable_id, 3), (healthy_id, 2)):
        user_id = await bootstrap.fetchval(
            """INSERT INTO platform.users (email, role, deleted_at)
               VALUES ($1, 'user', NOW() - INTERVAL '31 days') RETURNING id""",
            f"poison-{uuid.uuid4().hex}@example.com",
        )
        await bootstrap.execute(
            """INSERT INTO platform.erasure_requests
               (request_id, user_id, state, resume_state, eligible_at)
               VALUES ($1, $2, 'ready', 'learning_pending', NOW() - $3::interval)""",
            request_id,
            user_id,
            timedelta(days=offset),
        )
    # Only the healthy request carries the receipts finalization requires.
    await bootstrap.executemany(
        """INSERT INTO platform.erasure_acknowledgements
           (request_id, domain, receipt) VALUES ($1, $2, $3::jsonb)""",
        [
            (healthy_id, "qdrant", {"residual_points": 0}),
            (healthy_id, "research", {"acknowledged": True}),
            (healthy_id, "learning", {"acknowledged": True}),
        ],
    )
    due = [
        row["request_id"]
        for row in await executor.fetch(
            "SELECT request_id FROM platform.due_erasure_request_ids($1)", 20
        )
    ]
    assert due.index(unfinishable_id) < due.index(healthy_id)

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

    states = dict(
        (row["request_id"], row["state"])
        for row in await bootstrap.fetch(
            "SELECT request_id, state FROM platform.erasure_requests WHERE request_id = ANY($1)",
            [unfinishable_id, healthy_id],
        )
    )
    assert states == {unfinishable_id: "ready", healthy_id: "complete"}


async def test_a_revoked_finalize_privilege_is_visible_and_does_not_crash_the_loop(
    contract_pg_dsn: str,
    erasure_connections,
    caplog,
) -> None:
    """Losing the capability must report, not fail silently and not restart forever.

    The per-request isolation that keeps one unfinishable request from starving
    the rest also swallows a privilege loss, which is systemic rather than
    per-request. Every other privilege assertion in this file is made on a
    direct connection, so without this one nothing exercises that path through
    the executor itself and the pass would report a clean zero forever.
    """
    bootstrap, _ = erasure_connections
    request_id = uuid.uuid4()
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW() - INTERVAL '31 days') RETURNING id""",
        f"revoked-{uuid.uuid4().hex}@example.com",
    )
    await bootstrap.execute(
        """INSERT INTO platform.erasure_requests
           (request_id, user_id, state, resume_state, eligible_at)
           VALUES ($1, $2, 'ready', 'learning_pending', NOW() - INTERVAL '2 days')""",
        request_id,
        user_id,
    )

    await bootstrap.execute(
        "REVOKE EXECUTE ON FUNCTION platform.finalize_erasure(uuid) FROM jarvis_erasure_executor"
    )
    pool = await asyncpg.create_pool(
        contract_pg_dsn,
        user="jarvis_erasure_executor",
        password="erasure-executor-contract-password",
        min_size=1,
        max_size=1,
    )
    try:
        from platform_api.erasure_executor import finalize_due_requests

        with caplog.at_level(logging.ERROR):
            finalized = await finalize_due_requests(pool, limit=20)
    finally:
        await pool.close()
        await bootstrap.execute(
            "GRANT EXECUTE ON FUNCTION platform.finalize_erasure(uuid) TO jarvis_erasure_executor"
        )

    assert finalized == 0, "a request cannot be reported finalized without the capability"
    reported = [
        record
        for record in caplog.records
        if "Account erasure finalization failed" in record.message
        and record.exc_info is not None
        and isinstance(record.exc_info[1], asyncpg.InsufficientPrivilegeError)
    ]
    assert reported, (
        "the lost capability itself must reach the log — a bare 'finalization failed' is "
        "what an ordinary unfinishable request produces, so it cannot distinguish the two"
    )
    assert (
        await bootstrap.fetchval(
            "SELECT state FROM platform.erasure_requests WHERE request_id = $1", request_id
        )
        == "ready"
    ), "the request must remain due so it finalizes once the capability returns"


async def test_transition_capability_enforces_the_declared_state_graph(
    platform_runtime_connection,
) -> None:
    """Only state changes db/ownership-manifest.json declares are accepted.

    Every ordered pair of states is put through the capability and the refusals
    are recorded, so the graph is proved by invoking the database rather than by
    reading the declaration back to itself. Edges into 'complete' are carved
    out: the executor's finalization capability owns that step exclusively, and
    refusing it here is what keeps that ownership exclusive.
    """
    bootstrap, runtime = platform_runtime_connection
    declared = json.loads(
        (_REPO_ROOT / "db" / "ownership-manifest.json").read_text(encoding="utf-8")
    )["erasure"]
    states = declared["states"]
    expected = {
        (source, target)
        for source, targets in declared["transitions"].items()
        for target in targets
        if target != "complete"
    }
    assert "complete" in declared["transitions"]["executing"]

    # Deleted just now, so no state this walk reaches can become executor work.
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW()) RETURNING id""",
        f"graph-{uuid.uuid4().hex}@example.com",
    )
    request_id = uuid.uuid4()
    await bootstrap.execute(
        """INSERT INTO platform.erasure_requests
           (request_id, user_id, state, resume_state, eligible_at)
           VALUES ($1, $2, 'requested', 'qdrant_pending', NOW())""",
        request_id,
        user_id,
    )

    accepted: set[tuple[str, str]] = set()
    for source in states:
        for target in states:
            try:
                await runtime.fetchval(
                    "SELECT platform.transition_erasure_v1($1, $2, $3)",
                    request_id,
                    target,
                    source,
                )
            except asyncpg.RaiseError:
                continue
            accepted.add((source, target))

    assert accepted == expected
    assert ("requested", "ready") not in accepted
    assert ("executing", "complete") not in accepted
    with pytest.raises(asyncpg.RaiseError, match="reserved for finalization"):
        await runtime.fetchval(
            "SELECT platform.transition_erasure_v1($1, 'complete', 'executing')", request_id
        )


async def test_a_stranded_request_keeps_its_reason_and_resumes_with_a_fresh_budget(
    platform_runtime_connection,
) -> None:
    """Stranding preserves why, and resuming restores the retry budget.

    Clearing the reason on the way in leaves the operator with a stopped
    erasure and no cause, and resuming without clearing the exhausted attempt
    counter strands it again on the first failure instead of repairing it.
    """
    bootstrap, runtime = platform_runtime_connection
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW() - INTERVAL '31 days') RETURNING id""",
        f"stranded-{uuid.uuid4().hex}@example.com",
    )
    request_id = await runtime.fetchval("SELECT platform.request_erasure_v1($1)", user_id)
    await runtime.fetch("SELECT * FROM platform.begin_erasure_destructive_v1($1)", request_id)
    assert (
        await runtime.fetchval(
            "SELECT platform.record_erasure_retry_v1($1, 'qdrant_pending')", request_id
        )
        == "retry_wait"
    )
    assert (
        await runtime.fetchval(
            "SELECT platform.transition_erasure_v1($1, 'attention_required', 'retry_wait')",
            request_id,
        )
        == "attention_required"
    )
    stranded = await bootstrap.fetchrow(
        "SELECT state, attempts, last_error FROM platform.erasure_requests WHERE request_id = $1",
        request_id,
    )
    assert dict(stranded) == {
        "state": "attention_required",
        "attempts": 1,
        "last_error": "owner command unavailable",
    }

    resume_state = await runtime.fetchval("SELECT platform.resume_erasure_v1($1)", request_id)
    assert resume_state == "qdrant_pending"
    resumed = await bootstrap.fetchrow(
        "SELECT state, attempts, last_error FROM platform.erasure_requests WHERE request_id = $1",
        request_id,
    )
    assert dict(resumed) == {"state": "qdrant_pending", "attempts": 0, "last_error": None}

    # A newer unfinished request owns the account's erasure from here, and the
    # single-active-request index leaves stranded requests out on purpose.
    await runtime.fetchval(
        "SELECT platform.record_erasure_retry_v1($1, 'qdrant_pending')", request_id
    )
    await runtime.fetchval(
        "SELECT platform.transition_erasure_v1($1, 'attention_required', 'retry_wait')",
        request_id,
    )
    newer_id = await runtime.fetchval("SELECT platform.request_erasure_v1($1)", user_id)
    assert newer_id != request_id
    with pytest.raises(asyncpg.RaiseError, match="superseded by a newer request"):
        await runtime.fetchval("SELECT platform.resume_erasure_v1($1)", request_id)

    # 'executing' belongs to finalization, which runs in one transaction, so no
    # waiting request can record it as the phase to resume.
    with pytest.raises(asyncpg.RaiseError, match="erasure retry is not allowed"):
        await runtime.fetchval("SELECT platform.record_erasure_retry_v1($1, 'executing')", newer_id)


async def test_restore_refuses_an_account_whose_erasure_stranded(
    platform_runtime_connection,
) -> None:
    """A stranded erasure means domain data is already gone, so restore refuses.

    'attention_required' is reached only after every destructive attempt
    failed. Restoring past it would hand the user an account whose research and
    learning data no longer exists, and would delete the record of that.
    """
    bootstrap, runtime = platform_runtime_connection
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW() - INTERVAL '1 day') RETURNING id""",
        f"restore-{uuid.uuid4().hex}@example.com",
    )
    await bootstrap.execute(
        """INSERT INTO platform.erasure_requests
           (request_id, user_id, state, resume_state, attempts, eligible_at)
           VALUES ($1, $2, 'attention_required', 'learning_pending', 8, NOW())""",
        uuid.uuid4(),
        user_id,
    )

    with pytest.raises(asyncpg.RaiseError, match="erasure has already started"):
        await runtime.fetch("SELECT * FROM platform.restore_account_v1($1)", user_id)

    assert (
        await bootstrap.fetchval(
            "SELECT COUNT(*) FROM platform.erasure_requests WHERE user_id = $1", user_id
        )
        == 1
    )
    assert (
        await bootstrap.fetchval(
            "SELECT deleted_at IS NOT NULL FROM platform.users WHERE id = $1", user_id
        )
        is True
    )


async def test_an_acknowledgement_binds_to_a_phase_the_request_reached(
    platform_runtime_connection,
) -> None:
    """A receipt is durable only for a phase this request actually reached.

    Accepting one for a later phase would let the caller assemble the receipt
    set finalization counts before the owning service had run, and would leave
    an audit trail claiming work that never happened.
    """
    bootstrap, runtime = platform_runtime_connection
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW() - INTERVAL '31 days') RETURNING id""",
        f"ack-{uuid.uuid4().hex}@example.com",
    )
    request_id = await runtime.fetchval("SELECT platform.request_erasure_v1($1)", user_id)

    with pytest.raises(asyncpg.RaiseError, match="not allowed for this request"):
        await runtime.execute(
            "SELECT platform.record_erasure_ack_v1($1, 'qdrant', '{}'::jsonb)", request_id
        )

    await runtime.fetch("SELECT * FROM platform.begin_erasure_destructive_v1($1)", request_id)
    await runtime.execute(
        "SELECT platform.record_erasure_ack_v1($1, 'qdrant', '{}'::jsonb)", request_id
    )
    with pytest.raises(asyncpg.RaiseError, match="not allowed for this request"):
        await runtime.execute(
            "SELECT platform.record_erasure_ack_v1($1, 'learning', '{}'::jsonb)", request_id
        )

    # A phase already passed keeps accepting its receipt: a resumed pass replays
    # durable work rather than repeating it.
    await runtime.fetchval(
        "SELECT platform.transition_erasure_v1($1, 'research_pending', 'qdrant_pending')",
        request_id,
    )
    await runtime.execute(
        "SELECT platform.record_erasure_ack_v1($1, 'qdrant', '{\"replayed\": true}'::jsonb)",
        request_id,
    )
    assert await bootstrap.fetchval(
        """SELECT receipt FROM platform.erasure_acknowledgements
               WHERE request_id = $1 AND domain = 'qdrant'""",
        request_id,
    ) == {"replayed": True}


async def test_restore_and_the_destructive_start_share_one_lock_order(
    platform_runtime_connection,
) -> None:
    """Restore takes the erasure request row before the account row.

    Every erasure capability locks in that order. Opposite orders deadlock --
    one session holding the request row while the other holds the account row
    leaves each waiting on the other -- and no caller models SQLSTATE 40P01.
    """
    bootstrap, runtime = platform_runtime_connection
    user_id = await bootstrap.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW() - INTERVAL '1 day') RETURNING id""",
        f"lock-{uuid.uuid4().hex}@example.com",
    )
    request_id = uuid.uuid4()
    await bootstrap.execute(
        """INSERT INTO platform.erasure_requests
           (request_id, user_id, state, resume_state, eligible_at)
           VALUES ($1, $2, 'requested', 'qdrant_pending', NOW() + INTERVAL '29 days')""",
        request_id,
        user_id,
    )

    transaction = bootstrap.transaction()
    await transaction.start()
    committed = False
    try:
        await bootstrap.fetchrow(
            "SELECT request_id FROM platform.erasure_requests WHERE request_id = $1 FOR UPDATE",
            request_id,
        )
        restore = asyncio.create_task(
            runtime.fetch("SELECT * FROM platform.restore_account_v1($1)", user_id)
        )
        await asyncio.sleep(0.1)
        assert not restore.done()

        # The account row is still free because the restore has not reached it,
        # so taking it here waits on nothing instead of closing a lock cycle.
        await bootstrap.fetchrow("SELECT id FROM platform.users WHERE id = $1 FOR UPDATE", user_id)
        await transaction.commit()
        committed = True

        assert [row["id"] for row in await restore] == [user_id]
    finally:
        if not committed:
            await transaction.rollback()

    assert (
        await bootstrap.fetchval("SELECT deleted_at FROM platform.users WHERE id = $1", user_id)
        is None
    )
