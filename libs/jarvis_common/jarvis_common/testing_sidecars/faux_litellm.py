"""Deterministic real-HTTP sidecar for LiteLLM / OpenAI-compatible boundary tests.

Mimics the ``POST /v1/chat/completions`` and ``POST /v1/embeddings`` endpoints
that the production stack reaches through LiteLLM.  Tests enqueue scripted
responses via :meth:`FauxLiteLLMServer.add_response` and friends; the server
drains the queue in order, returning a default empty-JSON response when the
queue is exhausted.

Usage::

    async with FauxLiteLLMServer() as srv:
        srv.add_pydantic_response("smart", MyModel(answer="hi"))
        # point the app at srv.url and make requests
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Queue entry types
# ---------------------------------------------------------------------------


@dataclass
class _ChatEntry:
    content: str  # raw JSON string to embed in choices[0].message.content


@dataclass
class _StreamEntry:
    tokens: list[str]  # emitted as successive SSE data chunks


@dataclass
class _ErrorEntry:
    status_code: int
    detail: str = ""


_QueueEntry = _ChatEntry | _StreamEntry | _ErrorEntry

# ---------------------------------------------------------------------------
# Embedding helper (mirrors FauxOllamaServer.deterministic_embedding)
# ---------------------------------------------------------------------------


def _deterministic_embedding(text: str, *, dimension: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        values.extend((byte + 1) / 256.0 for byte in block)
        counter += 1
    return values[:dimension]


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

_DONE_LINE = b"data: [DONE]\n\n"


def _sse_chunk(delta_content: str, model: str) -> bytes:
    """Encode one streaming token as an SSE data line."""
    payload = {
        "id": f"chatcmpl-faux-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": delta_content},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_stop_chunk(model: str) -> bytes:
    payload = {
        "id": "chatcmpl-faux-stop",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


# ---------------------------------------------------------------------------
# FauxLiteLLMServer
# ---------------------------------------------------------------------------

_DEFAULT_CONTENT = "{}"  # empty JSON — surfaces missing-script as Instructor validation error


class FauxLiteLLMServer:
    """Aiohttp sidecar that speaks OpenAI-compatible chat + embeddings endpoints.

    Enqueue responses before each test; the server returns them in order.
    When the queue is empty, a default ``"{}"`` content is returned so tests
    fail with a clear Pydantic/Instructor validation error rather than a crash.
    """

    def __init__(self, *, dimension: int = 1024, host: str = "127.0.0.1") -> None:
        """Configure the embedding dimension and bind host; no server is started yet."""
        self._dimension = dimension
        self._host = host
        self._queues: dict[str, deque[_QueueEntry]] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._url: str | None = None

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> FauxLiteLLMServer:
        await self._start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stop()

    async def _start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handle_chat)
        app.router.add_post("/v1/embeddings", self._handle_embeddings)
        app.router.add_get("/v1/models", self._handle_models)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, 0)
        await site.start()
        if site._server is None or not site._server.sockets:  # noqa: SLF001
            await runner.cleanup()
            raise RuntimeError("FauxLiteLLMServer failed to bind a socket")
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001

        self._runner = runner
        self._site = site
        self._url = f"http://{self._host}:{port}"

    async def _stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        self._url = None
        if runner is not None:
            await runner.cleanup()

    # ------------------------------------------------------------------
    # Public scripting API
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """Base URL of the running server; raises ``RuntimeError`` when not started."""
        if self._url is None:
            raise RuntimeError("FauxLiteLLMServer is not running")
        return self._url

    def add_response(self, model: str, content: str) -> None:
        """Enqueue a raw JSON string to return as choices[0].message.content."""
        self._queue_for(model).append(_ChatEntry(content=content))

    def add_pydantic_response(self, model: str, instance: BaseModel) -> None:
        """Serialize *instance* to JSON and enqueue as a chat response."""
        self.add_response(model, instance.model_dump_json())

    def add_error(self, model: str, status_code: int, detail: str = "") -> None:
        """Enqueue an HTTP error response for the next chat request."""
        self._queue_for(model).append(_ErrorEntry(status_code=status_code, detail=detail))

    def add_stream_tokens(self, model: str, tokens: list[str]) -> None:
        """Enqueue a list of tokens to emit as SSE streaming chunks."""
        self._queue_for(model).append(_StreamEntry(tokens=list(tokens)))

    def reset(self) -> None:
        """Clear all queues (handy between tests sharing one fixture instance)."""
        self._queues.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _queue_for(self, model: str) -> deque[_QueueEntry]:
        if model not in self._queues:
            self._queues[model] = deque()
        return self._queues[model]

    def _pop_entry(self, model: str) -> _QueueEntry | None:
        q = self._queues.get(model)
        if q:
            return q.popleft()
        return None

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_chat(self, request: web.Request) -> web.StreamResponse | web.Response:
        payload = await request.json()
        model = payload.get("model", "smart")
        is_stream = bool(payload.get("stream", False))

        entry = self._pop_entry(model)

        # Error entry — always a plain error response regardless of stream flag
        if isinstance(entry, _ErrorEntry):
            return web.Response(
                status=entry.status_code,
                text=json.dumps({"error": {"message": entry.detail or "scripted error"}}),
                content_type="application/json",
            )

        if is_stream:
            return await self._streaming_response(request, entry, model)
        return self._non_streaming_response(entry, model)

    def _non_streaming_response(self, entry: _QueueEntry | None, model: str) -> web.Response:
        if isinstance(entry, _StreamEntry):
            content = "".join(entry.tokens)
        elif isinstance(entry, _ChatEntry):
            content = entry.content
        else:
            content = _DEFAULT_CONTENT

        body = {
            "id": f"chatcmpl-faux-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return web.json_response(body)

    async def _streaming_response(
        self, request: web.Request, entry: _QueueEntry | None, model: str
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)

        if isinstance(entry, _StreamEntry):
            tokens = entry.tokens
        elif isinstance(entry, _ChatEntry):
            tokens = [entry.content]
        else:
            tokens = [_DEFAULT_CONTENT]

        for token in tokens:
            await response.write(_sse_chunk(token, model))

        await response.write(_sse_stop_chunk(model))
        await response.write(_DONE_LINE)
        await response.write_eof()
        return response

    async def _handle_embeddings(self, request: web.Request) -> web.Response:
        payload = await request.json()
        raw_input = payload.get("input", [])
        texts = [raw_input] if isinstance(raw_input, str) else [str(t) for t in raw_input]
        model = payload.get("model", "embed")
        return web.json_response(
            {
                "object": "list",
                "model": model,
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": _deterministic_embedding(text, dimension=self._dimension),
                    }
                    for i, text in enumerate(texts)
                ],
            }
        )

    async def _handle_models(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {"id": "smart", "object": "model", "owned_by": "faux"},
                    {"id": "fast", "object": "model", "owned_by": "faux"},
                    {"id": "embed", "object": "model", "owned_by": "faux"},
                ],
            }
        )
