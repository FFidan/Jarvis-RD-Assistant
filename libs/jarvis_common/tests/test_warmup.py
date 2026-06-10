"""Unit tests for the startup model warm-up hook (U1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from jarvis_common.warmup import make_warmup_hook


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
