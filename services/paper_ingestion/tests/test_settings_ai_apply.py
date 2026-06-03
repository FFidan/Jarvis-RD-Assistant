"""Tests for ``apply_ai_settings`` Ollama pull/validate gating (W2-D).

The route must pull/validate the target Ollama model BEFORE mutating env /
LiteLLM config, so LiteLLM never routes to a not-yet-pulled model. vLLM and
other externally-served backends must skip the pull entirely.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from paper_ingestion.routers import settings_ai


class _FakeStreamResponse:
    """Async-context-manager mimicking ``httpx.AsyncClient.stream`` result."""

    def __init__(self, status_code: int, events: list[dict]):
        self.status_code = status_code
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for event in self._events:
            yield json.dumps(event)


class _FakeTagsResponse:
    def __init__(self, models: list[dict]):
        self._models = models

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"models": self._models}


class _FakeAsyncClient:
    """Records GET/stream calls so tests can assert pull behaviour + ordering."""

    def __init__(
        self, *, installed: list[str], pull_status: int = 200, pull_events: list[dict] | None = None
    ):
        self._installed = installed
        self._pull_status = pull_status
        self._pull_events = pull_events or [{"status": "success"}]
        self.calls: list[str] = []
        self.pull_names: list[str] = []

    async def get(self, url: str, timeout=None):  # noqa: ANN001
        self.calls.append(f"GET {url}")
        return _FakeTagsResponse([{"name": name} for name in self._installed])

    def stream(self, method: str, url: str, json=None, timeout=None):  # noqa: ANN001, A002
        self.calls.append(f"{method} {url}")
        if json is not None:
            self.pull_names.append(json.get("name"))
        return _FakeStreamResponse(self._pull_status, self._pull_events)


def _make_request(client) -> SimpleNamespace:  # noqa: ANN001
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=client)))


@pytest.fixture
def _patched(monkeypatch):
    """Patch gates, applier, tier, and response builder; return the apply spy."""
    monkeypatch.setattr(settings_ai, "resolve_candidates_for_tier", lambda *a, **k: object())
    monkeypatch.setattr(settings_ai, "candidate_is_allowed", lambda *a, **k: True)
    monkeypatch.setattr(settings_ai, "_effective_tier", lambda: "tier-test")

    apply_spy = MagicMock()
    monkeypatch.setattr(settings_ai._APPLIER, "apply", apply_spy)

    monkeypatch.setattr(
        settings_ai,
        "get_paper_ingestion_settings",
        lambda: SimpleNamespace(ollama_base_url="http://ollama:11434"),
    )

    sentinel = object()

    async def _fake_get_ai_settings(*a, **k):
        return sentinel

    monkeypatch.setattr(settings_ai, "get_ai_settings", _fake_get_ai_settings)
    return apply_spy


@pytest.mark.asyncio
async def test_absent_model_is_pulled_before_apply(_patched, monkeypatch):
    apply_spy = _patched
    order: list[str] = []
    apply_spy.side_effect = lambda *a, **k: order.append("apply")

    client = _FakeAsyncClient(installed=["other-model:latest"])
    req = settings_ai.ApplyRequest(backend="ollama", model="ollama/qwen3:8b")

    await settings_ai.apply_ai_settings(req, _make_request(client), _admin=None)

    # A pull was issued for the bare tag (ollama/ prefix stripped).
    assert client.pull_names == ["qwen3:8b"]
    assert any("POST http://ollama:11434/api/pull" in c for c in client.calls)
    # apply ran AFTER the pull stream completed.
    assert order == ["apply"]
    apply_spy.assert_called_once()
    pull_idx = next(i for i, c in enumerate(client.calls) if "/api/pull" in c)
    assert pull_idx == len(client.calls) - 1  # pull is the last network op


@pytest.mark.asyncio
async def test_present_model_skips_pull(_patched):
    apply_spy = _patched
    client = _FakeAsyncClient(installed=["qwen3:8b"])
    req = settings_ai.ApplyRequest(backend="ollama", model="ollama/qwen3:8b")

    await settings_ai.apply_ai_settings(req, _make_request(client), _admin=None)

    assert client.pull_names == []
    assert not any("/api/pull" in c for c in client.calls)
    apply_spy.assert_called_once()


@pytest.mark.asyncio
async def test_pull_failure_surfaces_502_and_skips_apply(_patched):
    apply_spy = _patched
    client = _FakeAsyncClient(installed=[], pull_status=500)
    req = settings_ai.ApplyRequest(backend="ollama", model="ollama/qwen3:8b")

    with pytest.raises(HTTPException) as excinfo:
        await settings_ai.apply_ai_settings(req, _make_request(client), _admin=None)

    assert excinfo.value.status_code == 502
    apply_spy.assert_not_called()


@pytest.mark.asyncio
async def test_pull_error_event_surfaces_502_and_skips_apply(_patched):
    apply_spy = _patched
    client = _FakeAsyncClient(
        installed=[], pull_status=200, pull_events=[{"error": "no such model"}]
    )
    req = settings_ai.ApplyRequest(backend="ollama", model="ollama/qwen3:8b")

    with pytest.raises(HTTPException) as excinfo:
        await settings_ai.apply_ai_settings(req, _make_request(client), _admin=None)

    assert excinfo.value.status_code == 502
    apply_spy.assert_not_called()


@pytest.mark.asyncio
async def test_pull_stream_without_success_event_surfaces_502(_patched):
    """A pull stream that ends without a {"status":"success"} terminal (e.g. a
    dropped/truncated connection) must NOT be read as complete — apply is skipped."""
    apply_spy = _patched
    client = _FakeAsyncClient(
        installed=[], pull_status=200, pull_events=[{"status": "pulling manifest"}]
    )
    req = settings_ai.ApplyRequest(backend="ollama", model="ollama/qwen3:8b")

    with pytest.raises(HTTPException) as excinfo:
        await settings_ai.apply_ai_settings(req, _make_request(client), _admin=None)

    assert excinfo.value.status_code == 502
    apply_spy.assert_not_called()


@pytest.mark.asyncio
async def test_non_ollama_backend_skips_tags_and_pull(_patched):
    apply_spy = _patched
    client = _FakeAsyncClient(installed=[])
    req = settings_ai.ApplyRequest(backend="vllm", model="some/vllm-model")

    await settings_ai.apply_ai_settings(req, _make_request(client), _admin=None)

    assert client.calls == []  # no /api/tags, no /api/pull
    apply_spy.assert_called_once()
