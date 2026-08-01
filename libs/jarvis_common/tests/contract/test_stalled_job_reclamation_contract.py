"""Real-DB contract: which ``doing`` jobs stalled-job reclamation actually selects.

Every unit test of the reclamation drives an ``AsyncMock`` job manager, so none
of them can observe procrastinate's real selection predicate — and that
predicate, not the reclamation loop, decides whether anything is ever
recovered. ``select_stalled_jobs_by_heartbeat`` matches a ``doing`` job when
the WORKER row it points at has a stale heartbeat, OR when it points at no
worker row at all (``worker_id IS NULL``, with no time condition; the foreign
key is ``ON DELETE SET NULL``, so a pruned or unregistered worker leaves its
jobs in exactly that state).

The three legs below pin that behaviour against the deployed schema on a real
pg16.8:

1. a job bound to a worker whose heartbeat is FRESH is NOT reclaimed — the
   reason a reclamation that only runs at worker start recovers nothing after
   an ordinary fast restart;
2. the SAME job, once that worker's heartbeat ages past the threshold, IS
   finished ``failed`` with the interruption message written to
   ``job_progress``;
3. a job whose ``worker_id`` is NULL is reclaimed immediately, fresh worker
   rows notwithstanding.

The procrastinate rows are committed (the job manager runs on its own
connection and could not otherwise see them) and removed in the teardown; the
``job_progress`` write goes through the ``contract_conn`` transaction and
vanishes with its rollback.
"""

# Verified: libs/jarvis_common/jarvis_common/app_factory.py:174

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
from jarvis_common.app_factory import _STALLED_HEARTBEAT_SECONDS, _reclaim_stalled_jobs
from jarvis_common.testing_db import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TASK_NAME = "stalled_job_reclamation_probe"


async def _new_worker(pg: asyncpg.Connection) -> int:
    """Register a worker row with the default (now) heartbeat."""
    return await pg.fetchval(
        "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id",
    )


async def _new_doing_job(
    pg: asyncpg.Connection, *, worker_id: int | None, jarvis_job_id: str
) -> int:
    """Seed a ``doing`` job carrying the JARVIS job id every enqueue path sets."""
    return await pg.fetchval(
        """
        INSERT INTO procrastinate_jobs (queue_name, task_name, args, status, worker_id)
        VALUES ('builtin', $1, jsonb_build_object('job_id', $2::text), 'doing', $3)
        RETURNING id
        """,
        _TASK_NAME,
        jarvis_job_id,
        worker_id,
    )


async def _job_status(pg: asyncpg.Connection, job_id: int) -> str:
    return await pg.fetchval("SELECT status::text FROM procrastinate_jobs WHERE id = $1", job_id)


async def _progress_error(conn: asyncpg.Connection, jarvis_job_id: str) -> dict[str, Any] | None:
    raw = await conn.fetchval(
        "SELECT error FROM job_progress WHERE jarvis_job_id = $1", jarvis_job_id
    )
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_reclamation_selects_stale_and_orphaned_jobs_only(
    contract_pg_dsn: str, contract_conn
) -> None:
    """A fresh worker row shields its job; a stale or absent one does not."""
    from procrastinate import App  # noqa: PLC0415
    from procrastinate.contrib.aiopg import AiopgConnector  # noqa: PLC0415

    procrastinate_app = App(connector=AiopgConnector(dsn=contract_pg_dsn))
    await procrastinate_app.open_async()
    pg = await asyncpg.connect(contract_pg_dsn)
    app = SimpleNamespace(
        state=SimpleNamespace(
            procrastinate_app=procrastinate_app,
            db_pool=SharedConnPool(contract_conn),
        )
    )
    worker_ids: list[int] = []
    try:
        worker_id = await _new_worker(pg)
        worker_ids.append(worker_id)
        job_id = await _new_doing_job(pg, worker_id=worker_id, jarvis_job_id="reclaim-bound")

        # Leg 1 — the worker row is fresh: the job is invisible to the sweep.
        await _reclaim_stalled_jobs(app)
        assert await _job_status(pg, job_id) == "doing"
        assert await _progress_error(contract_conn, "reclaim-bound") is None

        # Leg 2 — the same job, once the worker's heartbeat ages out.
        await pg.execute(
            """
            UPDATE procrastinate_workers
               SET last_heartbeat = NOW() - ($1 || ' seconds')::interval
             WHERE id = $2
            """,
            str(_STALLED_HEARTBEAT_SECONDS + 60),
            worker_id,
        )
        assert await _reclaim_stalled_jobs(app) >= 1
        assert await _job_status(pg, job_id) == "failed"
        error = await _progress_error(contract_conn, "reclaim-bound")
        assert error is not None
        assert error["code"] == "JOB_INTERRUPTED"

        # Leg 3 — no worker row at all: reclaimed regardless of any heartbeat.
        worker_ids.append(await _new_worker(pg))
        orphan_id = await _new_doing_job(pg, worker_id=None, jarvis_job_id="reclaim-orphan")
        assert await _reclaim_stalled_jobs(app) >= 1
        assert await _job_status(pg, orphan_id) == "failed"
        orphan_error = await _progress_error(contract_conn, "reclaim-orphan")
        assert orphan_error is not None
        assert orphan_error["code"] == "JOB_INTERRUPTED"
    finally:
        # Committed rows: events cascade with their job, and the worker rows go
        # once nothing references them.
        await pg.execute("DELETE FROM procrastinate_jobs WHERE task_name = $1", _TASK_NAME)
        await pg.execute(
            "DELETE FROM procrastinate_workers WHERE id = ANY($1::bigint[])", worker_ids
        )
        await pg.close()
        await procrastinate_app.close_async()
