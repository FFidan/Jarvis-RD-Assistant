"""Tests for the shared Langfuse lifespan hook (DOM-J-01).

Covers :func:`jarvis_common.app_factory.init_langfuse_hook` and
:func:`jarvis_common.app_factory.make_init_langfuse_hook`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from jarvis_common.app_factory import (
    init_langfuse_hook,
    make_init_langfuse_hook,
)


@pytest.mark.asyncio
async def test_init_langfuse_hook_attaches_openai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook builds an Instructor-patched OpenAI client and attaches it to app.state."""
    # LANGFUSE_HOST unset → _langfuse_lifespan_hook is a no-op (logs info).
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    fake_openai_client = MagicMock(name="instructor_patched_client")

    with (
        patch("instructor.from_openai", return_value=fake_openai_client) as mock_from_openai,
        patch("openai.AsyncOpenAI") as mock_async_openai,
    ):
        app = FastAPI()
        await init_langfuse_hook(app)

    assert app.state.openai_client is fake_openai_client
    # Instructor.from_openai was called with the AsyncOpenAI instance.
    mock_from_openai.assert_called_once()
    mock_async_openai.assert_called_once()


@pytest.mark.asyncio
async def test_init_langfuse_hook_invokes_set_services_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a set_services_callback is provided, it receives the openai client."""
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    fake_openai_client = MagicMock(name="instructor_patched_client")
    captured: list[Any] = []

    def _capture(client: Any) -> None:
        captured.append(client)

    with (
        patch("instructor.from_openai", return_value=fake_openai_client),
        patch("openai.AsyncOpenAI"),
    ):
        app = FastAPI()
        await init_langfuse_hook(app, set_services_callback=_capture)

    assert captured == [fake_openai_client]
    assert app.state.openai_client is fake_openai_client


@pytest.mark.asyncio
async def test_make_init_langfuse_hook_returns_bound_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``make_init_langfuse_hook`` produces a (FastAPI) -> Awaitable[None] hook."""
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    fake_openai_client = MagicMock(name="instructor_patched_client")
    seen: list[Any] = []

    hook = make_init_langfuse_hook(lambda c: seen.append(c))

    with (
        patch("instructor.from_openai", return_value=fake_openai_client),
        patch("openai.AsyncOpenAI"),
    ):
        app = FastAPI()
        await hook(app)

    assert seen == [fake_openai_client]
    assert app.state.openai_client is fake_openai_client


@pytest.mark.asyncio
async def test_init_langfuse_hook_uses_master_key_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LITELLM_MASTER_KEY is set, it's forwarded as the AsyncOpenAI ``api_key``."""
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-master-key")
    # Force settings re-evaluation; jarvis_common.settings caches via lru_cache.
    from jarvis_common.settings import get_secrets_settings  # noqa: PLC0415

    get_secrets_settings.cache_clear()

    with (
        patch("instructor.from_openai") as mock_from_openai,
        patch("openai.AsyncOpenAI") as mock_async_openai,
    ):
        app = FastAPI()
        await init_langfuse_hook(app)

    _, kwargs = mock_async_openai.call_args
    assert kwargs["api_key"] == "test-master-key"
    mock_from_openai.assert_called_once()

    # Reset cache so subsequent tests are unaffected.
    get_secrets_settings.cache_clear()


@pytest.mark.asyncio
async def test_init_langfuse_hook_falls_back_to_dummy_when_no_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LITELLM_MASTER_KEY is absent, AsyncOpenAI ``api_key="dummy"``."""
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    from jarvis_common.settings import get_secrets_settings  # noqa: PLC0415

    get_secrets_settings.cache_clear()

    with (
        patch("instructor.from_openai"),
        patch("openai.AsyncOpenAI") as mock_async_openai,
    ):
        app = FastAPI()
        await init_langfuse_hook(app)

    _, kwargs = mock_async_openai.call_args
    assert kwargs["api_key"] == "dummy"

    get_secrets_settings.cache_clear()
