"""Live-schema coverage for the orphaned job-progress purge.

The statement's whole contract is which rows survive it, and that is decided by
a correlated NOT EXISTS against ``procrastinate_jobs``. Only a real schema can
answer it: a mocked connection can show that a statement was issued, never that
it kept the progress row whose job is still around.
"""

from __future__ import annotations

import asyncpg
import pytest

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
        " VALUES ('paper_ingestion', 'contract.noop', jsonb_build_object('job_id', $1::text))",
        live_id,
    )
    await contract_conn.executemany(
        "INSERT INTO job_progress (jarvis_job_id, progress) VALUES ($1, 0.5)",
        [(live_id,), (orphan_id,)],
    )

    await contract_conn.execute(ORPHANED_JOB_PROGRESS_PURGE)

    assert await _progress_ids(contract_conn, [live_id, orphan_id]) == {live_id}
