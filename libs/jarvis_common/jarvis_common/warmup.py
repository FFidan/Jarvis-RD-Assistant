"""Startup model warm-up (U1, cold-start).

The first chat/RAG request a user makes is slow because the model is loaded on
demand. These hooks fire one tiny embedding and/or one 1-token chat through the
existing LiteLLM endpoint at startup so the model is resident (``keep_alive:-1``
is already set in the LiteLLM config) before the first real question.

Design: backend-agnostic (LiteLLM routes to ollama|vllm). The orchestration
(fire-and-forget, non-blocking boot, never fatal) lives here; each service
supplies its own warmers via a builder so jarvis_common never imports
service-specific embedding-model constants.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from jarvis_common.llm_client import build_litellm_headers, get_litellm_config

if TYPE_CHECKING:
    import httpx
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

LifespanHook = Callable[["FastAPI"], Awaitable[None]]
WarmerBuilder = Callable[["FastAPI"], list[Callable[[], Awaitable[Any]]]]


async def warm_chat_model(http_client: httpx.AsyncClient, model: str = "smart") -> None:
    """Send a 1-token chat completion so the chat model loads before first use."""
    config = get_litellm_config()
    resp = await http_client.post(
        f"{config.base_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0.0,
        },
        headers=build_litellm_headers(config),
        timeout=120.0,
    )
    resp.raise_for_status()


async def warm_embedding_model(http_client: httpx.AsyncClient, model_name: str) -> None:
    """Send a tiny embedding request so the embedding model loads before first use."""
    config = get_litellm_config()
    resp = await http_client.post(
        f"{config.base_url}/v1/embeddings",
        json={"model": model_name, "input": "warmup"},
        headers=build_litellm_headers(config),
        timeout=120.0,
    )
    resp.raise_for_status()


def make_warmup_hook(build_warmers: WarmerBuilder) -> LifespanHook:
    """Return a lifespan hook that warms models in the background.

    ``build_warmers(app)`` returns zero-arg async callables (e.g. an embedding
    warmer and a chat warmer) built from ``app.state``. The hook schedules them
    as a single fire-and-forget task: warm-up must never block boot, and a
    failure (model not ready, endpoint down) is logged and swallowed — it is a
    latency optimization, not a correctness requirement.
    """

    async def _hook(app: FastAPI) -> None:
        try:
            warmers = build_warmers(app)
        except Exception:
            logger.warning("warmup: could not build warmers — skipping", exc_info=True)
            return

        async def _run() -> None:
            for warmer in warmers:
                try:
                    await warmer()
                except Exception as exc:
                    logger.info("warmup: a model warm-up call failed (non-fatal): %r", exc)

        # Fire-and-forget; retained on app.state so the task is not GC'd mid-run.
        app.state.warmup_task = asyncio.create_task(_run(), name="model_warmup")

    return _hook
