"""Unit tests for jarvis_common.task_registry (B.4 Step 2 part 1).

These tests assert structural properties only:
  - All ``JOB_HANDLER_OWNER`` kinds register as procrastinate tasks.
  - Each task's ``queue`` matches the owning service from the same map.
  - The JobContext shim exposes the legacy contract (``update_progress``,
    ``is_cancelled``, ``job_id``).
  - Importing ``task_registry`` without first calling ``set_dependencies``
    is fine; the runtime check fires only when a task body executes.

No DB / connector / network calls are exercised — that is Step 2 part 2.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# All tasks registered under the dotted kind names
# ---------------------------------------------------------------------------


def test_all_tasks_registered() -> None:
    """``app.tasks`` should contain exactly the ``JOB_HANDLER_OWNER`` keys
    (filtering out procrastinate's two builtin ``remove_old_jobs`` aliases)."""
    from jarvis_common.jobs import JOB_HANDLER_OWNER
    from jarvis_common.task_registry import app

    user_task_names = {name for name, task in app.tasks.items() if task.queue != "builtin"}
    expected = set(JOB_HANDLER_OWNER.keys())

    assert user_task_names == expected, (
        f"missing: {expected - user_task_names}, unexpected: {user_task_names - expected}"
    )
    assert len(user_task_names) == len(JOB_HANDLER_OWNER)
    assert "model.pull" in user_task_names


# ---------------------------------------------------------------------------
# Queue assignment matches the service owner map
# ---------------------------------------------------------------------------


def test_queue_assignments_match_owner_map() -> None:
    """For each registered task, ``task.queue == JOB_HANDLER_OWNER[task.name]``."""
    from jarvis_common.jobs import JOB_HANDLER_OWNER
    from jarvis_common.task_registry import app

    for name, task in app.tasks.items():
        if task.queue == "builtin":
            continue  # procrastinate's auto-registered remove_old_jobs
        assert name in JOB_HANDLER_OWNER, f"task {name!r} not in JOB_HANDLER_OWNER"
        expected_queue = JOB_HANDLER_OWNER[name]
        assert task.queue == expected_queue, (
            f"task {name!r} has queue={task.queue!r} but owner map says {expected_queue!r}"
        )


# ---------------------------------------------------------------------------
# Shim implements the legacy JobContext surface
# ---------------------------------------------------------------------------


def test_ctx_shim_implements_jobcontext_protocol() -> None:
    """The shim must expose ``job_id``, ``update_progress``, ``is_cancelled``
    matching the legacy ``jarvis_common.jobs.JobContext`` shape."""
    from jarvis_common._ctx_shim import ProcrastinateJobContextShim, make_ctx_shim

    shim = make_ctx_shim(None, job_id="job-abc")

    # Attribute
    assert isinstance(shim, ProcrastinateJobContextShim)
    assert shim.job_id == "job-abc"

    # update_progress: async callable accepting (progress, message=None)
    assert callable(shim.update_progress)
    assert inspect.iscoroutinefunction(shim.update_progress)
    sig = inspect.signature(shim.update_progress)
    params = list(sig.parameters)
    assert params[:2] == ["progress", "message"], params

    # is_cancelled: async callable returning bool
    assert callable(shim.is_cancelled)
    assert inspect.iscoroutinefunction(shim.is_cancelled)


@pytest.mark.asyncio
async def test_ctx_shim_methods_runnable() -> None:
    """The Step 2 stub bodies must execute without raising."""
    from jarvis_common._ctx_shim import make_ctx_shim

    shim = make_ctx_shim(None, job_id="job-xyz")
    await shim.update_progress(0.5, "halfway")  # no-op log line
    assert (await shim.is_cancelled()) is False


# ---------------------------------------------------------------------------
# set_dependencies is not required at import time
# ---------------------------------------------------------------------------


def test_set_dependencies_then_called_pre_worker() -> None:
    """Registration happens at module import; ``set_dependencies`` is only
    enforced at task-execution time. Importing must succeed regardless."""
    import jarvis_common.task_registry as task_registry

    assert hasattr(task_registry, "app")
    assert hasattr(task_registry, "set_dependencies")
    # Tasks are populated even without set_dependencies having been called.
    assert any(t.queue != "builtin" for t in task_registry.app.tasks.values()), (
        "expected user tasks to be registered at import time"
    )


def test_require_dependencies_raises_before_set() -> None:
    """The task-body guard raises a clear RuntimeError when dependencies are
    not yet set. We test the guard directly to avoid running the worker."""
    import jarvis_common.task_registry as task_registry

    # Snapshot + reset module-level deps
    saved_pool = task_registry._pool
    saved_http = task_registry._http_client
    task_registry._pool = None
    task_registry._http_client = None
    try:
        with pytest.raises(RuntimeError, match="set_dependencies"):
            task_registry._require_dependencies()
    finally:
        task_registry._pool = saved_pool
        task_registry._http_client = saved_http


@pytest.mark.asyncio
async def test_run_legacy_handler_records_success_result(monkeypatch) -> None:
    """Task dispatch persists terminal success payloads returned by handlers."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = SimpleNamespace(record_terminal_outcome=AsyncMock())

    async def handler(_pool, _http_client, payload, _ctx):
        assert payload == {"paper_id": 7}
        return {"cards_created": 2}

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    result = await task_registry._run_legacy_handler(
        SimpleNamespace(),
        {"paper_id": 7},
        handler,
    )

    assert result == {"cards_created": 2}
    ctx.record_terminal_outcome.assert_awaited_once_with(result={"cards_created": 2})


@pytest.mark.asyncio
async def test_run_legacy_handler_records_job_error(monkeypatch) -> None:
    """Task dispatch persists JSON-safe JobError details before re-raising."""
    import jarvis_common.task_registry as task_registry
    from jarvis_common.jobs import JobError

    pool = object()
    http_client = object()
    ctx = SimpleNamespace(record_terminal_outcome=AsyncMock())

    async def handler(_pool, _http_client, _payload, _ctx):
        raise JobError("No chunks", action_link={"href": "/papers/1", "label": "Open"})

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    with pytest.raises(JobError):
        await task_registry._run_legacy_handler(SimpleNamespace(), {}, handler)

    ctx.record_terminal_outcome.assert_awaited_once_with(
        error={"message": "No chunks", "action_link": {"href": "/papers/1", "label": "Open"}}
    )


# ---------------------------------------------------------------------------
# Connector double-assign guard (task_registry.py:80 comment)
# ---------------------------------------------------------------------------


def test_connector_double_assign_requirement_documented() -> None:
    """Guard the requirement that services must assign the connector to BOTH
    ``procrastinate_app.connector`` AND ``procrastinate_app.job_manager.connector``
    before opening the app.

    This test verifies that ``app.job_manager`` exists on the module-level App
    and that its ``.connector`` attribute is the same object as ``app.connector``
    — the invariant that service lifespan startup must maintain.

    See: services/paper_ingestion/paper_ingestion/main.py lines 226-227 for
    the canonical double-assign pattern.
    """
    from jarvis_common.task_registry import app

    # After module import both references should already be consistent
    # (same AiopgConnector object constructed at module scope).
    assert hasattr(app, "job_manager"), (
        "procrastinate.App must expose .job_manager for connector re-assignment"
    )
    assert app.job_manager.connector is app.connector, (
        "app.connector and app.job_manager.connector must be the same object; "
        "service lifespan must assign BOTH when replacing the connector at startup "
        "(see main.py: procrastinate_app.connector = ...; "
        "procrastinate_app.job_manager.connector = procrastinate_app.connector)"
    )
