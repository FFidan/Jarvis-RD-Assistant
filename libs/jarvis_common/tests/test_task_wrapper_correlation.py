"""Tests for the task wrapper: sets correlation_id and emits job lifecycle events.

Verifies that ``_run_legacy_handler`` in ``jarvis_common.task_registry``:
  - Sets ``correlation_id_var`` to a UUID for the duration of the handler call.
  - Emits a "started" event before the handler runs.
  - Emits a "finished" event on handler success.
  - Emits a "failed" event (category=error) on handler exception.
  - Resets ``correlation_id_var`` after the call (via ContextVar token).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jarvis_common.logging_config import correlation_id_var
from jarvis_common.telemetry import configure_telemetry
from opentelemetry import trace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(job_id: str = "jarvis-job-uuid") -> SimpleNamespace:
    """Return a minimal ctx shim double."""
    return SimpleNamespace(
        job_id=job_id,
        record_terminal_outcome=AsyncMock(),
    )


def _make_procrastinate_context(task_name: str = "test.task") -> SimpleNamespace:
    """Return a minimal procrastinate.JobContext double."""
    job = SimpleNamespace(task_name=task_name, task_kwargs={"job_id": "jarvis-job-uuid"})
    return SimpleNamespace(job=job)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_wrapper_sets_correlation_id_var_for_handler(monkeypatch) -> None:
    """Handler executes with correlation_id_var set to a UUID instance."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = _make_ctx()
    captured_corr: list[uuid.UUID | None] = []

    async def handler(_pool, _http_client, _payload, _ctx):
        captured_corr.append(correlation_id_var.get())
        return {}

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    with patch("jarvis_common.task_registry.log_event", new=AsyncMock()):
        await task_registry._run_legacy_handler(
            _make_procrastinate_context(),
            {},
            handler,
        )

    assert len(captured_corr) == 1
    corr = captured_corr[0]
    assert corr is not None, "correlation_id_var should be set inside handler"
    assert isinstance(corr, uuid.UUID), f"Expected UUID, got {type(corr)}"


@pytest.mark.asyncio
async def test_task_wrapper_emits_started_event_with_job_id_in_context(monkeypatch) -> None:
    """log_event is called with message='started' and job_id in context."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = _make_ctx(job_id="my-jarvis-id")
    log_event_mock = AsyncMock()

    async def handler(_pool, _http_client, _payload, _ctx):
        return {}

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    with patch("jarvis_common.task_registry.log_event", new=log_event_mock):
        await task_registry._run_legacy_handler(
            _make_procrastinate_context("paper.process"),
            {},
            handler,
        )

    calls = log_event_mock.await_args_list
    started_calls = [c for c in calls if c.kwargs.get("message") == "started"]
    assert started_calls, "Expected at least one log_event call with message='started'"
    started_kwargs = started_calls[0].kwargs
    assert started_kwargs["category"] == "job"
    assert started_kwargs["level"] == "info"
    ctx_payload = started_kwargs.get("context", {})
    assert "job_id" in ctx_payload, f"Expected 'job_id' in context, got {ctx_payload}"
    assert ctx_payload["job_id"] == "my-jarvis-id"


@pytest.mark.asyncio
async def test_task_wrapper_emits_finished_event_on_success(monkeypatch) -> None:
    """log_event is called with message='finished' after a successful handler run."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = _make_ctx()
    log_event_mock = AsyncMock()

    async def handler(_pool, _http_client, _payload, _ctx):
        return {"cards_created": 3}

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    with patch("jarvis_common.task_registry.log_event", new=log_event_mock):
        result = await task_registry._run_legacy_handler(
            _make_procrastinate_context(),
            {},
            handler,
        )

    assert result == {"cards_created": 3}
    calls = log_event_mock.await_args_list
    finished_calls = [c for c in calls if c.kwargs.get("message") == "finished"]
    assert finished_calls, "Expected at least one log_event call with message='finished'"
    assert finished_calls[0].kwargs["category"] == "job"
    assert finished_calls[0].kwargs["level"] == "info"


@pytest.mark.asyncio
async def test_task_wrapper_emits_failed_event_on_exception(monkeypatch) -> None:
    """log_event is called with message='failed' and the exception is re-raised."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = _make_ctx()
    log_event_mock = AsyncMock()

    async def handler(_pool, _http_client, _payload, _ctx):
        raise ValueError("something went wrong")

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    with patch("jarvis_common.task_registry.log_event", new=log_event_mock):
        with pytest.raises(ValueError, match="something went wrong"):
            await task_registry._run_legacy_handler(
                _make_procrastinate_context(),
                {},
                handler,
            )

    calls = log_event_mock.await_args_list
    failed_calls = [c for c in calls if c.kwargs.get("message") == "failed"]
    assert failed_calls, "Expected at least one log_event call with message='failed'"
    failed_kwargs = failed_calls[0].kwargs
    assert failed_kwargs["category"] == "job"
    assert failed_kwargs["level"] == "error"
    assert "error" in failed_kwargs.get("context", {})


@pytest.mark.asyncio
async def test_task_wrapper_resets_correlation_id_var_after_handler(monkeypatch) -> None:
    """correlation_id_var is reset to its prior value after handler completes."""
    import jarvis_common.task_registry as task_registry

    pool = object()
    http_client = object()
    ctx = _make_ctx()
    prior_token = correlation_id_var.set(None)

    async def handler(_pool, _http_client, _payload, _ctx):
        return {}

    monkeypatch.setattr(task_registry, "_pool", pool)
    monkeypatch.setattr(task_registry, "_http_client", http_client)
    monkeypatch.setattr(task_registry, "make_ctx_shim", lambda context, pool: ctx)

    try:
        with patch("jarvis_common.task_registry.log_event", new=AsyncMock()):
            await task_registry._run_legacy_handler(
                _make_procrastinate_context(),
                {},
                handler,
            )
        # After the call, correlation_id_var should be reset to None (prior value)
        assert correlation_id_var.get() is None
    finally:
        correlation_id_var.reset(prior_token)


@pytest.mark.asyncio
async def test_enqueue_proxy_preserves_raw_task_registration_and_adds_context() -> None:
    """The enqueue facade carries context while the underlying task stays the registered task."""
    import jarvis_common.task_registry as task_registry

    configure_telemetry(service="test", enabled=False, otlp_endpoint=None, timeout_ms=1)
    raw_task = SimpleNamespace(defer_async=AsyncMock(return_value="queued"))
    proxy = task_registry._TaskEnqueueProxy(raw_task)

    with trace.get_tracer("test").start_as_current_span("request"):
        result = await proxy.defer_async(job_id="job", user_id=7)

    assert result == "queued"
    payload = raw_task.defer_async.await_args.kwargs
    assert payload["job_id"] == "job"
    assert payload["_jarvis_telemetry"]["traceparent"].startswith("00-")
