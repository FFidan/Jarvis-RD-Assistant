"""Behavioral tests for task registration and dependency wiring.

The suite uses isolated Procrastinate applications and in-memory collaborators;
it does not open database or network connections.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Task registration
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
    from jarvis_common.task_registry import _TASK_MAP, register_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    async def _dummy(pool, http_client, payload, ctx):
        return {}

    kind = "unit.test_kind_to_task_inject"
    try:
        register_tasks(fresh_app, mapping={kind: _dummy}, queue="test_q")
        assert kind in task_registry.KIND_TO_TASK
    finally:
        _TASK_MAP.pop(kind, None)


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


def _blocked_task(monkeypatch, reason: str):
    """Register one task whose maintenance check always reports *reason*."""
    import procrastinate
    from jarvis_common import task_registry
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())
    handler = AsyncMock(return_value={"ok": True})
    registry = task_registry.TaskRegistry(fresh_app)
    monkeypatch.setattr(task_registry, "maintenance_skip_reason", lambda _label: reason)
    registry.register_tasks({"test.egress": handler}, queue="test_queue")
    return fresh_app.tasks["test.egress"], handler


def _context_with_attempts(attempts: int) -> SimpleNamespace:
    """Return a JobContext-shaped stub carrying the attempt counter."""
    return SimpleNamespace(job=SimpleNamespace(attempts=attempts))


@pytest.mark.asyncio
async def test_registered_task_retries_without_running_handler_during_restore(monkeypatch) -> None:
    """Every generated task wrapper fails closed before resolving dependencies."""
    from jarvis_common.maintenance import OutboundEgressBlockedError

    task, handler = _blocked_task(monkeypatch, "restore")

    with pytest.raises(OutboundEgressBlockedError, match="restore state"):
        await task.func(_context_with_attempts(0), job_id="job-1")

    handler.assert_not_awaited()
    assert task.retry_strategy.max_attempts is None
    assert task.retry_strategy.wait == 30
    assert task.retry_strategy.retry_exceptions == {OutboundEgressBlockedError}


@pytest.mark.asyncio
async def test_restore_maintenance_still_retries_after_an_hour_of_attempts(monkeypatch) -> None:
    """Restore maintenance clears itself, so its retry budget stays unlimited."""
    from jarvis_common.maintenance import OutboundEgressBlockedError

    task, handler = _blocked_task(monkeypatch, "restore")

    with pytest.raises(OutboundEgressBlockedError) as raised:
        await task.func(_context_with_attempts(5000), job_id="job-1")

    from jarvis_common.jobs import JobError

    assert not isinstance(raised.value, JobError)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_quarantine_retries_while_inside_the_bound(monkeypatch) -> None:
    """A quarantined task keeps retrying until the bound, under the retryable type."""
    from jarvis_common.maintenance import (
        OutboundEgressBlockedError,
        OutboundQuarantineBlockedError,
    )

    task, handler = _blocked_task(monkeypatch, "quarantine")

    with pytest.raises(OutboundQuarantineBlockedError) as raised:
        await task.func(_context_with_attempts(119), job_id="job-1")

    # The retry strategy matches by isinstance, so the subclass inherits the budget.
    assert isinstance(raised.value, OutboundEgressBlockedError)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_quarantine_fails_the_job_once_the_bound_is_passed(monkeypatch) -> None:
    """Past the bound the job goes terminal with the acknowledgement it is waiting on."""
    from jarvis_common.jobs import JobError
    from jarvis_common.maintenance import OutboundEgressBlockedError
    from jarvis_common.task_registry import _terminal_error_payload

    task, handler = _blocked_task(monkeypatch, "quarantine")

    with pytest.raises(JobError) as raised:
        await task.func(_context_with_attempts(120), job_id="job-1")

    assert "acknowledge the restore" in str(raised.value)
    assert "has stopped retrying" in str(raised.value)
    # JobError is absent from retry_exceptions, so this is a terminal outcome
    # whose text survives into the payload rather than collapsing to "Job failed".
    assert task.retry_strategy.retry_exceptions == {OutboundEgressBlockedError}
    assert _terminal_error_payload(raised.value) == {"message": str(raised.value)}
    handler.assert_not_awaited()


# ---------------------------------------------------------------------------
# Shared service-registration guard (register_service_tasks)
# ---------------------------------------------------------------------------


def test_register_service_tasks_raises_on_multiple_owner_queues() -> None:
    """A mapping spanning two owner queues fails fast, labelled by service_label."""
    import procrastinate
    from jarvis_common.task_registry import register_service_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    async def _dummy(pool, http_client, payload, ctx):
        return {}

    # paper.process -> paper_ingestion queue; card.generate -> learning_engine queue.
    mapping = {"paper.process": _dummy, "card.generate": _dummy}
    with pytest.raises(RuntimeError, match="svc.demo: KIND_TO_HANDLER spans multiple"):
        register_service_tasks(fresh_app, mapping, service_label="svc.demo")


def test_register_service_tasks_missing_kind_uses_service_label(monkeypatch) -> None:
    """When registration is a no-op, the missing-kind guard raises with service_label."""
    import jarvis_common.task_registry as tr
    import procrastinate
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    async def _dummy(pool, http_client, payload, ctx):
        return {}

    monkeypatch.setattr(tr, "register_tasks", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="svc.demo: failed to register kinds"):
        tr.register_service_tasks(fresh_app, {"card.generate": _dummy}, service_label="svc.demo")


# ---------------------------------------------------------------------------
# Importing the registry does not add service tasks
# ---------------------------------------------------------------------------


def test_no_service_tasks_at_import_time() -> None:
    """Importing the registry leaves service tasks unregistered."""
    from jarvis_common.jobs import JOB_HANDLER_OWNER
    from jarvis_common.task_registry import app

    user_task_names = {name for name, task in app.tasks.items() if task.queue not in ("builtin",)}
    # The decorator registers noop.test; service registrars add all other tasks.
    for kind in JOB_HANDLER_OWNER:
        assert kind not in user_task_names, (
            f"kind {kind!r} should not be in app.tasks at import time "
            f"(tasks are registered lazily by each service)"
        )


# ---------------------------------------------------------------------------
# Queue assignment: use register_tasks to validate owner map
# ---------------------------------------------------------------------------


def test_queue_for_kind_matches_owner_map() -> None:
    """queue_for_kind is the cross-service enqueue source of truth (mirrors the owner map)."""
    import pytest
    from jarvis_common.jobs import JOB_HANDLER_OWNER, queue_for_kind

    for kind, queue in JOB_HANDLER_OWNER.items():
        assert queue_for_kind(kind) == queue

    # zotero.push is the cross-service defer target used by learning_engine.
    assert queue_for_kind("zotero.push") == "paper_ingestion"

    with pytest.raises(KeyError):
        queue_for_kind("definitely.not.a.kind")


def test_queue_assignments_match_owner_map() -> None:
    """Bind every registered service task to its declared owner queue."""
    import procrastinate
    from jarvis_common.jobs import JOB_HANDLER_OWNER
    from learning_engine._task_register import register_learning_engine_tasks
    from paper_ingestion._task_register import register_paper_ingestion_tasks
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())
    register_paper_ingestion_tasks(fresh_app)
    register_learning_engine_tasks(fresh_app)

    for name, task in fresh_app.tasks.items():
        if task.queue == "builtin" or name == "noop.test":
            continue
        assert name in JOB_HANDLER_OWNER, f"task {name!r} not in JOB_HANDLER_OWNER"
        assert task.queue == JOB_HANDLER_OWNER[name], (
            f"task {name!r} registered on queue={task.queue!r} but owner map says "
            f"{JOB_HANDLER_OWNER[name]!r} — the worker would consume the wrong queue"
        )


# ---------------------------------------------------------------------------
# Context adapter behavior
# ---------------------------------------------------------------------------


def test_ctx_shim_implements_jobcontext_protocol() -> None:
    """Expose the job identifier, progress callback, and cancellation check."""
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
    """The context shim methods execute without external dependencies."""
    from jarvis_common._ctx_shim import make_ctx_shim

    shim = make_ctx_shim(None, job_id="job-xyz")
    await shim.update_progress(0.5, "halfway")  # no-op log line
    assert (await shim.is_cancelled()) is False


# ---------------------------------------------------------------------------
# Dependencies are required only when a task runs
# ---------------------------------------------------------------------------


def test_set_dependencies_then_called_pre_worker() -> None:
    """``set_dependencies`` is only enforced at task-execution time.
    Importing must succeed regardless; the public API is present on the module.
    """
    import jarvis_common.task_registry as task_registry

    assert hasattr(task_registry, "app")
    assert hasattr(task_registry, "set_dependencies")
    assert hasattr(task_registry, "register_tasks"), (
        "register_tasks must be exported for service startup hooks"
    )
    # Each service registers its tasks during startup.
    assert "noop.test" in task_registry.app.tasks or all(
        t.queue == "builtin" for t in task_registry.app.tasks.values()
    ), "unexpected non-builtin tasks at import time (check for accidental static registration)"


def test_require_dependencies_raises_before_set() -> None:
    """Raise a clear error when task dependencies have not been set."""
    import jarvis_common.task_registry as task_registry

    # Preserve module state while exercising the unset case.
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
    ctx = SimpleNamespace(job_id="test-uuid-1", record_terminal_outcome=AsyncMock())

    async def handler(_pool, _http_client, payload, _ctx):
        assert payload == {"paper_id": 7}
        return {"cards_created": 2}

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)
    monkeypatch.setattr(task_registry, "log_event", AsyncMock())

    result = await task_registry._run_legacy_handler(
        SimpleNamespace(),
        {"paper_id": 7},
        handler,
    )

    assert result == {"cards_created": 2}
    ctx.record_terminal_outcome.assert_awaited_once_with(
        result={"cards_created": 2}, is_error=False
    )


@pytest.mark.asyncio
async def test_run_legacy_handler_records_job_error(monkeypatch) -> None:
    """Task dispatch persists JSON-safe JobError details before re-raising."""
    import jarvis_common.task_registry as task_registry
    from jarvis_common.jobs import JobError

    pool = object()
    http_client = object()
    ctx = SimpleNamespace(job_id="test-uuid-2", record_terminal_outcome=AsyncMock())

    async def handler(_pool, _http_client, _payload, _ctx):
        raise JobError("No chunks", action_link={"href": "/papers/1", "label": "Open"})

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)
    monkeypatch.setattr(task_registry, "log_event", AsyncMock())

    with pytest.raises(JobError):
        await task_registry._run_legacy_handler(SimpleNamespace(), {}, handler)

    ctx.record_terminal_outcome.assert_awaited_once_with(
        error={"message": "No chunks", "action_link": {"href": "/papers/1", "label": "Open"}},
        is_error=True,
    )


@pytest.mark.asyncio
async def test_run_legacy_handler_redacts_unexpected_exception_text(monkeypatch) -> None:
    """Unexpected exceptions are logged server-side, not persisted verbatim for clients."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = SimpleNamespace(job_id="test-uuid-3", record_terminal_outcome=AsyncMock())

    async def handler(_pool, _http_client, _payload, _ctx):
        raise RuntimeError("secret token at /tmp/provider-body")

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)
    monkeypatch.setattr(task_registry, "log_event", AsyncMock())

    with pytest.raises(RuntimeError):
        await task_registry._run_legacy_handler(SimpleNamespace(), {}, handler)

    ctx.record_terminal_outcome.assert_awaited_once()
    error_payload = ctx.record_terminal_outcome.await_args.kwargs["error"]
    assert error_payload["message"] == "Job failed"
    assert error_payload["code"] == "JOB_FAILED"
    assert "secret" not in str(error_payload)
    assert "/tmp/provider-body" not in str(error_payload)


@pytest.mark.asyncio
async def test_run_legacy_handler_propagates_cancellation_without_persisting(monkeypatch) -> None:
    """A cancelled handler must re-raise CancelledError without persisting a failure."""
    import asyncio

    import jarvis_common.task_registry as task_registry

    ctx = SimpleNamespace(job_id="test-uuid-4", record_terminal_outcome=AsyncMock())

    async def handler(_pool, _http_client, _payload, _ctx):
        raise asyncio.CancelledError()

    monkeypatch.setattr(task_registry, "_pool", object())
    monkeypatch.setattr(task_registry, "_http_client", object())
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)
    monkeypatch.setattr(task_registry, "log_event", AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await task_registry._run_legacy_handler(SimpleNamespace(), {}, handler)

    ctx.record_terminal_outcome.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# Isolation + immutability (migrated from test_task_registry_xarch001.py)
# ---------------------------------------------------------------------------


def test_isolated_registrations_do_not_bleed() -> None:
    """Two independent TaskRegistry instances maintain separate kind→task maps."""
    import jarvis_common.task_registry as tr

    class _FakeApp:
        def __init__(self) -> None:
            self.tasks: dict[str, object] = {}

        def task(self, *, name: str, queue: str, pass_context: bool, retry):
            def _deco(fn):
                fn.queue = queue
                fn.retry_strategy = retry
                self.tasks[name] = fn
                return fn

            return _deco

    async def _dummy(_pool, _http, _payload, _ctx):
        return {}

    app_a = _FakeApp()
    app_b = _FakeApp()
    registry_a = tr.TaskRegistry(app_a)  # type: ignore[arg-type]
    registry_b = tr.TaskRegistry(app_b)  # type: ignore[arg-type]

    registry_a.register_tasks({"only.in_a": _dummy}, queue="qa")
    registry_b.register_tasks({"only.in_b": _dummy}, queue="qb")

    assert "only.in_a" in registry_a.kind_to_task
    assert "only.in_b" not in registry_a.kind_to_task
    assert "only.in_b" in registry_b.kind_to_task
    assert "only.in_a" not in registry_b.kind_to_task


def test_kind_to_task_is_immutable() -> None:
    """Reject direct writes to the read-only task mapping."""
    import jarvis_common.task_registry as tr

    with pytest.raises(TypeError):
        tr.KIND_TO_TASK["_should_not_write"] = object()  # type: ignore[index]


# ---------------------------------------------------------------------------
# Dependency propagation for additional app instances
# ---------------------------------------------------------------------------


def test_register_tasks_propagates_dependencies_via_set_dependencies() -> None:
    """Keep configured dependencies available when registering another app."""
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as tr
    import procrastinate
    from jarvis_common.task_registry import TaskDependencies
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    pool = MagicMock(name="pool")
    http_client = MagicMock(name="http_client")

    # Preserve the default registry while supplying dependencies for the test.
    saved_deps = tr._DEFAULT_REGISTRY._dependencies
    tr._DEFAULT_REGISTRY._dependencies = TaskDependencies(pool=pool, http_client=http_client)
    try:

        async def _dummy(_pool, _http, _payload, _ctx):
            return {}

        tr.register_tasks(fresh_app, mapping={"dep.test_copy": _dummy}, queue="test_q")

        # The default registry retains the same dependency objects.
        deps = tr._DEFAULT_REGISTRY.require_dependencies()
        assert deps.pool is pool
        assert deps.http_client is http_client
    finally:
        tr._DEFAULT_REGISTRY._dependencies = saved_deps
        tr._TASK_MAP.pop("dep.test_copy", None)


def test_register_tasks_no_error_when_default_dependencies_none() -> None:
    """Register another app when no default dependencies are configured."""
    import jarvis_common.task_registry as tr
    import procrastinate
    from procrastinate.contrib.aiopg import AiopgConnector

    fresh_app = procrastinate.App(connector=AiopgConnector())

    saved_deps = tr._DEFAULT_REGISTRY._dependencies
    tr._DEFAULT_REGISTRY._dependencies = None
    try:

        async def _dummy(_pool, _http, _payload, _ctx):
            return {}

        tr.register_tasks(fresh_app, mapping={"dep.test_none": _dummy}, queue="test_q")
    finally:
        tr._DEFAULT_REGISTRY._dependencies = saved_deps
        tr._TASK_MAP.pop("dep.test_none", None)
