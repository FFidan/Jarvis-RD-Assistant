"""Tests for ProcrastinateJobContextShim — progress UPSERT and cancel bridge.

Covers the wiring added when ctx_shim graduated from a Step-2 stub:
- ``update_progress`` UPSERTs into ``job_progress`` when a pool is supplied,
  and silently degrades to a no-op when no pool / job_id is present, or when
  the table is missing (older DB without migration 054).
- ``is_cancelled`` calls ``procrastinate.JobContext.should_abort()``.
- ``make_ctx_shim`` prefers ``task_kwargs['job_id']`` (JARVIS UUID) over the
  procrastinate bigint ``job.id``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# job_id derivation
# ---------------------------------------------------------------------------


def test_make_ctx_shim_prefers_task_kwargs_job_id() -> None:
    """When task_kwargs carries a JARVIS UUID, the shim uses it (not the bigint)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    job = SimpleNamespace(id=12345, task_kwargs={"job_id": "uuid-abc-123"})
    proc_ctx = SimpleNamespace(job=job)

    shim = make_ctx_shim(proc_ctx)

    assert shim.job_id == "uuid-abc-123"


def test_make_ctx_shim_falls_back_to_bigint_when_kwarg_missing() -> None:
    """When task_kwargs lacks job_id, falls back to str(procrastinate.job.id)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    job = SimpleNamespace(id=12345, task_kwargs={"paper_id": 7})
    proc_ctx = SimpleNamespace(job=job)

    shim = make_ctx_shim(proc_ctx)

    assert shim.job_id == "12345"


def test_make_ctx_shim_handles_none_task_kwargs() -> None:
    """task_kwargs may be None (procrastinate v3 default); fall back gracefully."""
    from jarvis_common._ctx_shim import make_ctx_shim

    job = SimpleNamespace(id=999, task_kwargs=None)
    proc_ctx = SimpleNamespace(job=job)

    shim = make_ctx_shim(proc_ctx)

    assert shim.job_id == "999"


def test_make_ctx_shim_no_ctx_returns_empty_job_id() -> None:
    """No procrastinate context, no job_id override → empty string (legacy contract)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    shim = make_ctx_shim(None)

    assert shim.job_id == ""


# ---------------------------------------------------------------------------
# update_progress — UPSERT path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_progress_upserts_into_job_progress() -> None:
    """When pool + job_id are present, UPSERT runs against job_progress."""
    from jarvis_common._ctx_shim import make_ctx_shim

    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    shim = make_ctx_shim(None, job_id="uuid-1", pool=pool)

    await shim.update_progress(0.5, "halfway")

    pool.execute.assert_awaited_once()
    sql, *args = pool.execute.await_args.args
    assert "INSERT INTO job_progress" in sql
    assert "ON CONFLICT (jarvis_job_id) DO UPDATE" in sql
    assert args == ["uuid-1", 0.5, "halfway"]


@pytest.mark.asyncio
async def test_update_progress_noop_without_pool() -> None:
    """No pool → no DB call (logs only)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    shim = make_ctx_shim(None, job_id="uuid-1")
    # Should not raise — purely logs at debug.
    await shim.update_progress(0.25, "quarter")


@pytest.mark.asyncio
async def test_update_progress_noop_without_job_id() -> None:
    """Empty job_id → no DB call even with a pool (defensive)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    shim = make_ctx_shim(None, job_id="", pool=pool)

    await shim.update_progress(0.1, "starting")

    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_progress_swallows_db_errors() -> None:
    """Progress reporting must never kill the job (defensive try/except)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    pool = AsyncMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("boom"))
    shim = make_ctx_shim(None, job_id="uuid-1", pool=pool)

    # Should not raise even though execute() blew up.
    await shim.update_progress(0.5, "halfway")
    pool.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_progress_coerces_int_progress_to_float() -> None:
    """Handler bodies that pass integers (e.g. 0, 100) shouldn't trip the SQL bind."""
    from jarvis_common._ctx_shim import make_ctx_shim

    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    shim = make_ctx_shim(None, job_id="uuid-1", pool=pool)

    await shim.update_progress(100, None)

    _sql, _job_id, progress, message = pool.execute.await_args.args
    assert isinstance(progress, float)
    assert progress == 100.0
    assert message is None


# ---------------------------------------------------------------------------
# is_cancelled — should_abort bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_cancelled_returns_false_without_ctx() -> None:
    """No procrastinate context → always False (unit-test convenience)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    shim = make_ctx_shim(None, job_id="uuid-1")

    assert (await shim.is_cancelled()) is False


@pytest.mark.asyncio
async def test_is_cancelled_calls_should_abort_truthy() -> None:
    """When should_abort() returns True, the shim reports cancelled."""
    from jarvis_common._ctx_shim import make_ctx_shim

    proc_ctx = SimpleNamespace(
        job=SimpleNamespace(id=1, task_kwargs={"job_id": "uuid-1"}),
        should_abort=lambda: True,
    )
    shim = make_ctx_shim(proc_ctx)

    assert (await shim.is_cancelled()) is True


@pytest.mark.asyncio
async def test_is_cancelled_calls_should_abort_falsy() -> None:
    """When should_abort() returns False, the shim reports not-cancelled."""
    from jarvis_common._ctx_shim import make_ctx_shim

    proc_ctx = SimpleNamespace(
        job=SimpleNamespace(id=1, task_kwargs={"job_id": "uuid-1"}),
        should_abort=lambda: False,
    )
    shim = make_ctx_shim(proc_ctx)

    assert (await shim.is_cancelled()) is False


@pytest.mark.asyncio
async def test_is_cancelled_swallows_should_abort_errors() -> None:
    """If should_abort() somehow raises, default to False (don't kill the job)."""
    from jarvis_common._ctx_shim import make_ctx_shim

    def _boom() -> bool:
        raise RuntimeError("transient")

    proc_ctx = SimpleNamespace(
        job=SimpleNamespace(id=1, task_kwargs={"job_id": "uuid-1"}),
        should_abort=_boom,
    )
    shim = make_ctx_shim(proc_ctx)

    assert (await shim.is_cancelled()) is False
