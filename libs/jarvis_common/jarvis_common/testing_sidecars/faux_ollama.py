"""Deterministic real-HTTP sidecar for local LLM boundary tests.

The product now reaches local models through LiteLLM's OpenAI-compatible API,
while older tests and docs still refer to Ollama's native ``/api/*`` routes.
This sidecar intentionally serves both shapes on a loopback TCP port so tests
exercise real HTTP without requiring GPU models, Docker, or outbound network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web


def deterministic_embedding(text: str, *, dimension: int) -> list[float]:
    """Return a stable non-zero embedding vector for *text*."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        values.extend((byte + 1) / 256.0 for byte in block)
        counter += 1
    return values[:dimension]


@dataclass
class FauxOllamaServer:
    """A small aiohttp server that mimics Ollama and LiteLLM success responses."""

    dimension: int = 8
    chat_responses: dict[str, str] = field(default_factory=dict)
    host: str = "127.0.0.1"

    _runner: web.AppRunner | None = field(default=None, init=False, repr=False)
    _site: web.TCPSite | None = field(default=None, init=False, repr=False)
    _url: str | None = field(default=None, init=False)

    @property
    def url(self) -> str:
        """Base URL of the running server; raises ``RuntimeError`` when not started."""
        if self._url is None:
            raise RuntimeError("FauxOllamaServer is not running")
        return self._url

    @property
    def base_url(self) -> str:
        """Alias for ``url``; provided for compatibility with httpx ``base_url`` kwargs."""
        return self.url

    async def __aenter__(self) -> FauxOllamaServer:
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        """Bind the aiohttp server to a random loopback port and register routes."""
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_post("/api/embed", self._ollama_embed)
        app.router.add_post("/api/embeddings", self._ollama_embed)
        app.router.add_post("/api/chat", self._ollama_chat)
        app.router.add_post("/v1/embeddings", self._openai_embeddings)
        app.router.add_post("/v1/chat/completions", self._openai_chat)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, 0)
        await site.start()
        if site._server is None or not site._server.sockets:  # type: ignore[attr-defined]  # noqa: SLF001
            await runner.cleanup()
            raise RuntimeError("FauxOllamaServer failed to bind a socket")
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]  # noqa: SLF001

        self._runner = runner
        self._site = site
        self._url = f"http://{self.host}:{port}"

    async def stop(self) -> None:
        """Shut down the aiohttp runner and release the bound port."""
        runner = self._runner
        self._runner = None
        self._site = None
        self._url = None
        if runner is not None:
            await runner.cleanup()

    async def _ollama_embed(self, request: web.Request) -> web.Response:
        payload = await request.json()
        raw_input = payload.get("input", payload.get("prompt", ""))
        texts = _coerce_texts(raw_input)
        vectors = [deterministic_embedding(text, dimension=self.dimension) for text in texts]
        body: dict[str, Any]
        if len(vectors) == 1 and "prompt" in payload:
            body = {"embedding": vectors[0]}
        else:
            body = {"embeddings": vectors}
        body.update({"model": payload.get("model", "faux-embed"), "done": True})
        return web.json_response(body)

    async def _openai_embeddings(self, request: web.Request) -> web.Response:
        payload = await request.json()
        texts = _coerce_texts(payload.get("input", []))
        return web.json_response(
            {
                "object": "list",
                "model": payload.get("model", "embed"),
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": deterministic_embedding(text, dimension=self.dimension),
                    }
                    for index, text in enumerate(texts)
                ],
            }
        )

    async def _ollama_chat(self, request: web.Request) -> web.Response:
        payload = await request.json()
        content = self._chat_content(payload.get("messages", []), payload.get("prompt"))
        return web.json_response(
            {
                "model": payload.get("model", "faux-chat"),
                "message": {"role": "assistant", "content": content},
                "done": True,
            }
        )

    async def _openai_chat(self, request: web.Request) -> web.Response:
        payload = await request.json()
        content = self._chat_content(payload.get("messages", []), payload.get("prompt"))
        return web.json_response(
            {
                "id": "chatcmpl-faux",
                "object": "chat.completion",
                "model": payload.get("model", "smart"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            }
        )

    def _chat_content(self, messages: Any, prompt: Any = None) -> str:
        text = ""
        if isinstance(prompt, str):
            text = prompt
        elif isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                text = str(last.get("content", ""))
        return self.chat_responses.get(text, f"faux response: {text}")


def _coerce_texts(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Iterable):
        return [str(item) for item in raw]
    return [str(raw)]
