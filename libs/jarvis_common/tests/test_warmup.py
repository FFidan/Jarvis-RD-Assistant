"""Unit tests for the startup model warm-up hook."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from jarvis_common import warmup as warmup_module
from jarvis_common.maintenance import OutboundEgressBlockedError
from jarvis_common.warmup import make_warmup_hook, warm_chat_model, warm_embedding_model


@pytest.mark.asyncio
async def test_warmup_hook_runs_each_warmer_in_background() -> None:
    calls: list[str] = []

    async def embed_warmer() -> None:
        calls.append("embed")

    async def chat_warmer() -> None:
        calls.append("chat")

    app = SimpleNamespace(state=SimpleNamespace())
    hook = make_warmup_hook(lambda _app: [embed_warmer, chat_warmer])

    await hook(app)  # type: ignore[arg-type]
    # Hook is non-blocking — the task is scheduled, not awaited inside the hook.
    await app.state.warmup_task

    assert calls == ["embed", "chat"]


@pytest.mark.asyncio
async def test_warmup_hook_is_non_fatal_when_a_warmer_raises() -> None:
    calls: list[str] = []

    async def failing_warmer() -> None:
        raise RuntimeError("model not ready")

    async def ok_warmer() -> None:
        calls.append("ok")

    app = SimpleNamespace(state=SimpleNamespace())
    hook = make_warmup_hook(lambda _app: [failing_warmer, ok_warmer])

    # Must not raise even though the first warmer fails.
    await hook(app)  # type: ignore[arg-type]
    await app.state.warmup_task

    # The failure is swallowed and subsequent warmers still run.
    assert calls == ["ok"]


@pytest.mark.asyncio
async def test_warmup_hook_does_not_block_boot_on_builder_error() -> None:
    def bad_builder(_app: Any) -> Any:
        raise ValueError("bad app state")

    app = SimpleNamespace(state=SimpleNamespace())
    hook = make_warmup_hook(bad_builder)

    # A builder error is logged and swallowed; no task is scheduled.
    await hook(app)  # type: ignore[arg-type]
    assert not hasattr(app.state, "warmup_task")
    await asyncio.sleep(0)  # let the loop settle; no pending warmup task


@pytest.mark.parametrize("warmer_name", ["chat", "embedding"])
@pytest.mark.asyncio
async def test_model_warmers_refuse_quarantine_before_configuration_or_http(
    warmer_name, tmp_path, monkeypatch
) -> None:
    """Warm-up requests fail closed before reading configuration or using HTTP."""
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    requests = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    def unexpected_config_read() -> None:
        raise AssertionError("configuration must remain unread during quarantine")

    monkeypatch.setattr(warmup_module, "get_litellm_config", unexpected_config_read)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        with pytest.raises(OutboundEgressBlockedError, match="credential review"):
            if warmer_name == "chat":
                await warm_chat_model(client)
            else:
                await warm_embedding_model(client, "embedding")

    assert requests == 0


@pytest.mark.asyncio
async def test_warmup_hook_keeps_quarantine_failures_non_fatal(tmp_path, monkeypatch) -> None:
    """The background hook swallows blocked warm-ups without touching HTTP."""
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    requests = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        app = SimpleNamespace(state=SimpleNamespace())
        hook = make_warmup_hook(
            lambda _app: [
                lambda: warm_embedding_model(client, "embedding"),
                lambda: warm_chat_model(client),
            ]
        )

        await hook(app)  # type: ignore[arg-type]
        await app.state.warmup_task

    assert requests == 0
