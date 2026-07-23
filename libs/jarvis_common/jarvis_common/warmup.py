"""Non-blocking startup warm-up for configured language models.

The first chat or retrieval request can be slow while its model loads. These
hooks schedule a minimal request through the configured LiteLLM endpoint, which
may reduce that initial delay. Startup does not wait for the request, and each
service supplies its own warmers without adding service-specific model
configuration to :mod:`jarvis_common`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from jarvis_common.llm_client import build_litellm_headers, get_litellm_config
from jarvis_common.maintenance import ensure_outbound_egress_allowed

if TYPE_CHECKING:
    import httpx
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

LifespanHook = Callable[["FastAPI"], Awaitable[None]]
WarmerBuilder = Callable[["FastAPI"], list[Callable[[], Awaitable[Any]]]]


async def warm_chat_model(http_client: httpx.AsyncClient, model: str = "smart") -> None:
    """Load a chat model with a minimal completion request.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Client used to reach the configured LiteLLM endpoint.
    model : str
        LiteLLM model alias to warm.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials await review before the request.
    httpx.HTTPStatusError
        If the warm-up endpoint returns a non-success status.
    """
    ensure_outbound_egress_allowed("chat model warm-up")
    config = get_litellm_config()
    url = f"{config.base_url}/v1/chat/completions"
    headers = build_litellm_headers(config)
    ensure_outbound_egress_allowed("chat model warm-up")
    resp = await http_client.post(
        url,
        json={
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0.0,
        },
        headers=headers,
        timeout=120.0,
    )
    resp.raise_for_status()


async def warm_embedding_model(http_client: httpx.AsyncClient, model_name: str) -> None:
    """Load an embedding model with a minimal embedding request.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Client used to reach the configured LiteLLM endpoint.
    model_name : str
        LiteLLM embedding-model alias to warm.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials await review before the request.
    httpx.HTTPStatusError
        If the warm-up endpoint returns a non-success status.
    """
    ensure_outbound_egress_allowed("embedding model warm-up")
    config = get_litellm_config()
    url = f"{config.base_url}/v1/embeddings"
    headers = build_litellm_headers(config)
    ensure_outbound_egress_allowed("embedding model warm-up")
    resp = await http_client.post(
        url,
        json={"model": model_name, "input": "warmup"},
        headers=headers,
        timeout=120.0,
    )
    resp.raise_for_status()


def make_warmup_hook(build_warmers: WarmerBuilder) -> LifespanHook:
    """Build a lifespan hook that warms models in the background.

    Parameters
    ----------
    build_warmers : WarmerBuilder
        Factory that returns zero-argument asynchronous warmers for an
        application.

    Returns
    -------
    LifespanHook
        Hook that schedules the warmers without blocking startup.

    Notes
    -----
    Warm-up failures are logged and ignored because warm-up affects latency,
    not service correctness. The task is retained on ``app.state`` until it
    finishes.
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

        # Keep the task reachable through app.state until it finishes.
        app.state.warmup_task = asyncio.create_task(_run(), name="model_warmup")

    return _hook
