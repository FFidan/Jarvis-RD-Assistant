"""Live-schema coverage for the orphaned job-progress purge.

The statement's whole contract is which rows survive it, and that is decided by
a correlated NOT EXISTS against ``procrastinate_jobs``. Only a real schema can
answer it: a mocked connection can show that a statement was issued, never that
it kept the progress row whose job is still around.
"""

from __future__ import annotations

import asyncpg
import procrastinate
import pytest
from procrastinate.contrib.aiopg import AiopgConnector
from jarvis_common.db_helpers import init_pg_connection

from paper_ingestion.scheduler import ORPHANED_JOB_PROGRESS_PURGE

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _progress_ids(conn: asyncpg.Connection, ids: list[str]) -> set[str]:
    rows = await conn.fetch(
        "SELECT jarvis_job_id FROM job_progress WHERE jarvis_job_id = ANY($1::text[])", ids
    )
    return {row["jarvis_job_id"] for row in rows}


# Verified: services/paper_ingestion/paper_ingestion/scheduler.py:513
async def test_purge_removes_only_progress_rows_whose_job_is_gone(
    contract_conn: asyncpg.Connection,
) -> None:
    """A progress row outlives its job only until the next prune cycle."""
    live_id = "contract-live-job"
    orphan_id = "contract-orphan-job"
    await contract_conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args)"
        " VALUES ('paper_ingestion', 'paper.process', jsonb_build_object('job_id', $1::text))",
        live_id,
    )
    await contract_conn.executemany(
        "INSERT INTO job_progress (jarvis_job_id, progress) VALUES ($1, 0.5)",
        [(live_id,), (orphan_id,)],
    )

    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_research_runtime")
    try:
        await contract_conn.execute(ORPHANED_JOB_PROGRESS_PURGE)
    finally:
        await contract_conn.execute("RESET SESSION AUTHORIZATION")

    assert await _progress_ids(contract_conn, [live_id, orphan_id]) == {live_id}


async def test_research_retention_preserves_learning_job_history(
    contract_pg_dsn: str,
    _contract_pool: asyncpg.Pool,
) -> None:
    """The real Procrastinate cleanup filters to Research's owning queue."""
    seed = await asyncpg.connect(contract_pg_dsn)
    await init_pg_connection(seed)
    await seed.execute("SET search_path TO ops, public")
    learning_job: int | None = None
    try:
        research_job = await seed.fetchval(
            """INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
               VALUES ('paper_ingestion', 'paper.process', '{}'::jsonb, 'succeeded')
               RETURNING id"""
        )
        learning_job = await seed.fetchval(
            """INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
               VALUES ('learning_engine', 'card.generate', '{}'::jsonb, 'succeeded')
               RETURNING id"""
        )
        await seed.executemany(
            """INSERT INTO ops.procrastinate_events (job_id, type, at)
               VALUES ($1, 'succeeded', NOW() - INTERVAL '31 days')""",
            [(research_job,), (learning_job,)],
        )

        cleanup_app = procrastinate.App(
            connector=AiopgConnector(dsn=contract_pg_dsn, options="-c search_path=ops,public")
        )
        async with cleanup_app.open_async():
            await cleanup_app.job_manager.delete_old_jobs(
                nb_hours=30 * 24,
                queue="paper_ingestion",
                include_failed=True,
                include_cancelled=True,
                include_aborted=True,
            )

        assert (
            await seed.fetchval(
                "SELECT EXISTS(SELECT 1 FROM ops.procrastinate_jobs WHERE id = $1)",
                research_job,
            )
            is False
        )
        assert (
            await seed.fetchval(
                "SELECT EXISTS(SELECT 1 FROM ops.procrastinate_jobs WHERE id = $1)",
                learning_job,
            )
            is True
        )
    finally:
        if learning_job is not None:
            await seed.execute("DELETE FROM ops.procrastinate_jobs WHERE id = $1", learning_job)
        await seed.close()
