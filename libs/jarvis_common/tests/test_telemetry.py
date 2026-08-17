"""Pure W3C context and bounded telemetry tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from jarvis_common import app_factory, telemetry
from jarvis_common.logging_config import correlation_id_var
from jarvis_common.telemetry import (
    _JarvisSpanExporter,
    capture_task_context,
    configure_telemetry,
    pop_task_context,
    request_span,
    restored_correlation,
    trace_headers,
)
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExportResult


def test_w3c_traceparent_is_continued_and_malformed_state_is_replaced() -> None:
    """A valid parent is continued while malformed trace state starts a new trace."""
    configure_telemetry(service="test", enabled=False, otlp_endpoint=None, timeout_ms=1)
    inbound = {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}

    with request_span(headers=inbound, service="test", method="GET"):
        continued = trace_headers()["traceparent"]
    with request_span(headers={"traceparent": "invalid"}, service="test", method="GET"):
        replaced = trace_headers()["traceparent"]

    assert continued.split("-")[1] == inbound["traceparent"].split("-")[1]
    assert replaced.split("-")[1] != inbound["traceparent"].split("-")[1]


def test_task_context_round_trip_strips_reserved_fields_from_handler_payload() -> None:
    """Worker restoration retains only W3C/correlation metadata outside domain payload."""
    configure_telemetry(service="test", enabled=False, otlp_endpoint=None, timeout_ms=1)
    corr = uuid.uuid4()
    token = correlation_id_var.set(corr)
    try:
        with trace.get_tracer("test").start_as_current_span("enqueue"):
            payload = capture_task_context({"paper_id": 42})
    finally:
        correlation_id_var.reset(token)

    carrier, restored_corr = pop_task_context(payload)

    assert payload == {"paper_id": 42}
    assert carrier["traceparent"].startswith("00-")
    assert restored_corr == corr


def test_restored_correlation_replaces_malformed_persisted_value() -> None:
    """Malformed persisted metadata cannot become an active correlation identifier."""
    with restored_correlation("not-a-uuid"):
        assert correlation_id_var.get() is not None
        assert correlation_id_var.get() != "not-a-uuid"


def test_generic_exporter_rejects_generation_spans_at_its_boundary() -> None:
    """Only spans created by the JARVIS telemetry scope reach generic OTLP export."""
    exported: list[object] = []

    class _SpyExporter:
        def export(self, spans):
            exported.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

    service_span = SimpleNamespace(
        instrumentation_scope=SimpleNamespace(name="jarvis_common.telemetry")
    )
    generation_span = SimpleNamespace(instrumentation_scope=SimpleNamespace(name="langfuse"))
    result = _JarvisSpanExporter(_SpyExporter()).export([generation_span, service_span])

    assert result is SpanExportResult.SUCCESS
    assert exported == [service_span]


def test_repeated_configuration_keeps_one_process_provider_after_prior_setup() -> None:
    """Repeated lifespan setup keeps the installed provider available for later starts."""
    configure_telemetry(service="first", enabled=False, otlp_endpoint=None, timeout_ms=1)
    first = trace.get_tracer_provider()
    configure_telemetry(service="second", enabled=False, otlp_endpoint=None, timeout_ms=1)

    assert trace.get_tracer_provider() is first


def test_lifecycle_flush_does_not_shutdown_the_process_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application teardowns flush boundedly while SDK atexit owns shutdown."""
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    force_flush = MagicMock(return_value=True)
    shutdown = MagicMock()
    monkeypatch.setattr(provider, "force_flush", force_flush)
    monkeypatch.setattr(provider, "shutdown", shutdown)
    monkeypatch.setattr(telemetry.trace, "get_tracer_provider", lambda: provider)

    telemetry.flush_telemetry(timeout_ms=17)

    force_flush.assert_called_once_with(timeout_millis=17)
    shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_lifespans_and_failed_startup_leave_the_process_provider_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan teardown never shuts down the provider needed by a later app."""
    pool = SimpleNamespace(close=AsyncMock())
    client = SimpleNamespace(aclose=AsyncMock())
    settings = SimpleNamespace(
        observability_enabled=False,
        otel_exporter_otlp_traces_endpoint=None,
        otel_export_timeout_ms=1,
        postgres_user="test",
        postgres_password_file="/tmp/test-password",
    )
    monkeypatch.setattr(app_factory, "validate_production_config", lambda: None)
    monkeypatch.setattr(app_factory, "reload_fernet_on_sighup", lambda: None)
    monkeypatch.setattr(app_factory, "get_jarvis_common_settings", lambda: settings)
    monkeypatch.setattr(app_factory, "build_database_url", lambda **_: "postgresql://test")
    monkeypatch.setattr(app_factory, "_resolve_db_pool_kwargs", lambda _: {})
    monkeypatch.setattr(app_factory.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(app_factory, "check_migrations", AsyncMock(return_value=object()))
    monkeypatch.setattr(app_factory, "validate_encrypted_config_rows", AsyncMock())
    monkeypatch.setattr(app_factory.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(app_factory, "_log_auth_status", lambda: None)
    flush_telemetry = MagicMock()
    monkeypatch.setattr(app_factory, "flush_telemetry", flush_telemetry)

    config = app_factory.ServiceLifespanConfig(service_name="telemetry-test")
    lifespan = app_factory.configure_lifespan(config)
    configure_telemetry(service="test", enabled=False, otlp_endpoint=None, timeout_ms=1)
    provider = trace.get_tracer_provider()

    async with lifespan(FastAPI()):
        assert trace.get_tracer_provider() is provider
    async with lifespan(FastAPI()):
        assert trace.get_tracer_provider() is provider

    async def fail_startup(_: FastAPI) -> None:
        raise RuntimeError("startup failed")

    failing = app_factory.configure_lifespan(
        app_factory.ServiceLifespanConfig(
            service_name="telemetry-test",
            custom_init_tasks=[fail_startup],
            custom_teardown_tasks=[None],
        )
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        async with failing(FastAPI()):
            pass

    assert trace.get_tracer_provider() is provider
    assert flush_telemetry.call_count == 3
    assert all(call.kwargs == {"timeout_ms": 1} for call in flush_telemetry.call_args_list)
