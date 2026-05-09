"""Tests for explicit task registry runtime dependencies."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class _FakeProcrastinateApp:
    """Minimal Procrastinate app double that records decorated tasks."""

    def __init__(self) -> None:
        self.tasks: dict[str, object] = {}

    def task(self, *, name: str, queue: str, pass_context: bool):
        """Return a decorator matching the Procrastinate task-registration shape."""

        def _decorate(fn):
            fn.queue = queue
            fn.pass_context = pass_context
            self.tasks[name] = fn
            return fn

        return _decorate


def _make_context(task_name: str = "demo.task") -> SimpleNamespace:
    """Return a minimal Procrastinate context double."""
    job = SimpleNamespace(task_name=task_name, task_kwargs={"job_id": "jarvis-job"})
    return SimpleNamespace(job=job)


@pytest.mark.asyncio
async def test_task_registry_dispatch_uses_explicit_dependencies() -> None:
    """An isolated registry dispatches handlers with its own runtime dependencies."""
    from jarvis_common.task_registry import TaskRegistry

    app = _FakeProcrastinateApp()
    registry = TaskRegistry(app)  # type: ignore[arg-type]
    pool = object()
    http_client = object()
    ctx = SimpleNamespace(job_id="jarvis-job", record_terminal_outcome=AsyncMock())
    captured: dict[str, object] = {}

    async def _handler(handler_pool, handler_http_client, payload, handler_ctx):
        captured["pool"] = handler_pool
        captured["http_client"] = handler_http_client
        captured["payload"] = payload
        captured["ctx"] = handler_ctx
        return {"ok": True}

    registry.set_dependencies(pool, http_client)  # type: ignore[arg-type]
    registry.register_tasks({"demo.task": _handler}, queue="demo")

    task = registry.kind_to_task["demo.task"]
    with (
        patch("jarvis_common.task_registry.make_ctx_shim", return_value=ctx),
        patch("jarvis_common.task_registry.log_event", new=AsyncMock()),
    ):
        result = await task(_make_context(), answer=42)

    assert result == {"ok": True}
    assert captured == {
        "pool": pool,
        "http_client": http_client,
        "payload": {"answer": 42},
        "ctx": ctx,
    }


def test_task_registry_requires_dependencies_before_dispatch() -> None:
    """The registry reports startup-order mistakes before a task runs."""
    from jarvis_common.task_registry import TaskRegistry

    registry = TaskRegistry(_FakeProcrastinateApp())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="set_dependencies"):
        registry.require_dependencies()
