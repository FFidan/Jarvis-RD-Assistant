"""Unit tests for jarvis_common.task_registry (B.4 Step 2 part 1 / W4-1).

These tests assert structural properties only:
  - ``register_tasks`` registers kind→handler entries on ``app.tasks``.
  - ``KIND_TO_TASK`` is populated by ``register_tasks`` calls.
  - Queue names passed to ``register_tasks`` are honoured by registered tasks.
  - The JobContext shim exposes the legacy contract (``update_progress``,
    ``is_cancelled``, ``job_id``).
  - Importing ``task_registry`` without first calling ``set_dependencies``
    is fine; the runtime check fires only when a task body executes.

W4-1 note: tasks are no longer registered at module import time. They are
registered by each service during lifespan startup via ``register_tasks``.
The structural tests below use a fresh ``procrastinate.App`` instance to avoid
polluting the module-level singleton.

No DB / connector / network calls are exercised — that is Step 2 part 2.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# register_tasks API — tasks registered on demand (W4-1)
# ---------------------------------------------------------------------------


def test_register_tasks_populates_app_tasks() -> None:
    """``register_tasks`` should register each kind as an ``app.task`` entry."""
    import procrastinate
    from jarvis_common.task_registry import register_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    async def _dummy_handler(pool, http_client, payload, ctx):
        return {}

    mapping = {
        "test.alpha": _dummy_handler,
        "test.beta": _dummy_handler,
    }
    register_tasks(fresh_app, mapping=mapping, queue="test_queue")

    registered_names = set(fresh_app.tasks.keys())
    assert "test.alpha" in registered_names
    assert "test.beta" in registered_names


def test_register_tasks_honours_queue() -> None:
    """Tasks registered via ``register_tasks`` use the supplied queue name."""
    import procrastinate
    from jarvis_common.task_registry import register_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    async def _dummy(pool, http_client, payload, ctx):
        return {}

    register_tasks(fresh_app, mapping={"svc.do_thing": _dummy}, queue="my_service")

    task = fresh_app.tasks["svc.do_thing"]
    assert task.queue == "my_service"


def test_register_tasks_populates_kind_to_task() -> None:
    """``register_tasks`` should insert the task objects into ``KIND_TO_TASK``."""
    import jarvis_common.task_registry as task_registry
    import procrastinate
    from jarvis_common.task_registry import register_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    async def _dummy(pool, http_client, payload, ctx):
        return {}

    kind = "unit.test_kind_to_task_inject"
    register_tasks(fresh_app, mapping={kind: _dummy}, queue="test_q")

    assert kind in task_registry.KIND_TO_TASK


def test_register_tasks_handler_closure() -> None:
    """Each kind must invoke its own handler (no late-binding bug)."""
    import procrastinate
    from jarvis_common.task_registry import register_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())
    calls: list[str] = []

    async def handler_a(pool, http_client, payload, ctx):
        calls.append("a")
        return {}

    async def handler_b(pool, http_client, payload, ctx):
        calls.append("b")
        return {}

    register_tasks(fresh_app, mapping={"t.a": handler_a, "t.b": handler_b}, queue="q")

    # Verify the bound handler is different for each task (closure captured correctly).
    task_a = fresh_app.tasks["t.a"]
    task_b = fresh_app.tasks["t.b"]
    # Each task's default-arg _h should reference its own handler.
    import inspect

    sig_a = inspect.signature(task_a.func)
    sig_b = inspect.signature(task_b.func)
    default_a = sig_a.parameters["_h"].default
    default_b = sig_b.parameters["_h"].default
    assert default_a is handler_a, "task_a should be bound to handler_a"
    assert default_b is handler_b, "task_b should be bound to handler_b"


# ---------------------------------------------------------------------------
# At import time app.tasks has only noop.test + builtin tasks (W4-1)
# ---------------------------------------------------------------------------


def test_no_service_tasks_at_import_time() -> None:
    """After W4-1: jarvis_common no longer registers service tasks at import.

    The module-level app should contain only the ``noop.test`` task and
    procrastinate's builtin tasks immediately after import — no paper_ingestion
    or learning_engine tasks.
    """
    from jarvis_common.jobs import JOB_HANDLER_OWNER
    from jarvis_common.task_registry import app

    user_task_names = {name for name, task in app.tasks.items() if task.queue not in ("builtin",)}
    # noop.test is registered unconditionally; service kinds are NOT registered.
    for kind in JOB_HANDLER_OWNER:
        assert kind not in user_task_names, (
            f"kind {kind!r} should NOT be in app.tasks at import time after W4-1 "
            f"(tasks are registered lazily by each service)"
        )


# ---------------------------------------------------------------------------
# Queue assignment: use register_tasks to validate owner map
# ---------------------------------------------------------------------------


def test_queue_assignments_match_owner_map() -> None:
    """register_tasks with JOB_HANDLER_OWNER keys and matching queues passes assertion."""
    import procrastinate
    from jarvis_common.jobs import JOB_HANDLER_OWNER
    from jarvis_common.task_registry import register_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    async def _dummy(pool, http_client, payload, ctx):
        return {}

    # Group by queue and register each group, then verify queue assignments.

    by_queue: dict[str, dict] = {}
    for kind, queue in JOB_HANDLER_OWNER.items():
        by_queue.setdefault(queue, {})[kind] = _dummy

    for queue, mapping in by_queue.items():
        register_tasks(fresh_app, mapping=mapping, queue=queue)

    for name, task in fresh_app.tasks.items():
        if task.queue == "builtin":
            continue
        if name == "noop.test":
            continue
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
    """``set_dependencies`` is only enforced at task-execution time.
    Importing must succeed regardless; the public API is present on the module."""
    import jarvis_common.task_registry as task_registry

    assert hasattr(task_registry, "app")
    assert hasattr(task_registry, "set_dependencies")
    assert hasattr(task_registry, "register_tasks"), (
        "register_tasks must be exported for service startup hooks (W4-1)"
    )
    # W4-1: tasks are registered lazily by each service — NOT at import time.
    # Only builtin tasks + noop.test exist immediately after import.
    assert "noop.test" in task_registry.app.tasks or all(
        t.queue == "builtin" for t in task_registry.app.tasks.values()
    ), "unexpected non-builtin tasks at import time (check for accidental static registration)"


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


@pytest.mark.asyncio
async def test_run_legacy_handler_redacts_unexpected_exception_text(monkeypatch) -> None:
    """Unexpected exceptions are logged server-side, not persisted verbatim for clients."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = SimpleNamespace(record_terminal_outcome=AsyncMock())

    async def handler(_pool, _http_client, _payload, _ctx):
        raise RuntimeError("secret token at /tmp/provider-body")

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    with pytest.raises(RuntimeError):
        await task_registry._run_legacy_handler(SimpleNamespace(), {}, handler)

    ctx.record_terminal_outcome.assert_awaited_once()
    error_payload = ctx.record_terminal_outcome.await_args.kwargs["error"]
    assert error_payload["message"] == "Job failed"
    assert error_payload["code"] == "JOB_FAILED"
    assert "secret" not in str(error_payload)
    assert "/tmp/provider-body" not in str(error_payload)


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
