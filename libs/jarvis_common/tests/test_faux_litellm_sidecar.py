"""Boundary-adapter tests for FauxLiteLLMServer sidecar.

Shape: boundary-adapter.
Each test verifies OUR adapter (the faux sidecar) behaves correctly for
OpenAI-compatible clients, exercising real HTTP over loopback.
"""

from __future__ import annotations

import json

import httpx
import pytest
from jarvis_common.testing_sidecars import FauxLiteLLMServer
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Shared model used across tests
# ---------------------------------------------------------------------------


class _Answer(BaseModel):
    answer: str
    score: int = 0


# ---------------------------------------------------------------------------
# Test 1: lifecycle
# ---------------------------------------------------------------------------


async def test_server_starts_and_stops_cleanly() -> None:
    """Context manager binds a port and exposes a valid http://127.0.0.1:<port> URL.

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:130
    """
    async with FauxLiteLLMServer() as srv:
        url = srv.url
        assert url.startswith("http://127.0.0.1:")
        port = int(url.rsplit(":", 1)[1])
        assert 1024 <= port <= 65535

    # After exit the URL property raises
    with pytest.raises(RuntimeError, match="not running"):
        _ = srv.url


# ---------------------------------------------------------------------------
# Test 2: non-streaming scripted content
# ---------------------------------------------------------------------------


async def test_chat_completions_returns_scripted_content() -> None:
    """add_response enqueues raw JSON returned in choices[0].message.content.

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:176
    """
    async with FauxLiteLLMServer() as srv:
        srv.add_response("smart", '{"answer": "hi", "score": 7}')

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{srv.url}/v1/chat/completions",
                json={"model": "smart", "messages": [{"role": "user", "content": "q"}]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == '{"answer": "hi", "score": 7}'
        assert body["object"] == "chat.completion"


# ---------------------------------------------------------------------------
# Test 3: Instructor / Pydantic round-trip
# ---------------------------------------------------------------------------


async def test_chat_completions_returns_pydantic_via_instructor() -> None:
    """add_pydantic_response + Instructor client returns a validated Pydantic instance.

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:180
    # Verified: libs/jarvis_common/jarvis_common/llm_client.py:357
    """
    import instructor
    import openai

    expected = _Answer(answer="instructor works", score=42)

    async with FauxLiteLLMServer() as srv:
        srv.add_pydantic_response("smart", expected)

        oc = instructor.from_openai(
            openai.AsyncOpenAI(base_url=f"{srv.url}/v1", api_key="dummy"),
            mode=instructor.Mode.JSON,
        )
        result = await oc.chat.completions.create(
            model="smart",
            response_model=_Answer,
            messages=[{"role": "user", "content": "give me an answer"}],
        )

    assert isinstance(result, _Answer)
    assert result.answer == "instructor works"
    assert result.score == 42


# ---------------------------------------------------------------------------
# Test 4: streaming SSE tokens
# ---------------------------------------------------------------------------


async def test_chat_completions_stream_emits_sse_tokens() -> None:
    """add_stream_tokens emits correct SSE chunks and a [DONE] terminator.

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:188
    """
    async with FauxLiteLLMServer() as srv:
        srv.add_stream_tokens("smart", ["hello ", "world"])

        collected: list[str] = []
        done_seen = False

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{srv.url}/v1/chat/completions",
                json={
                    "model": "smart",
                    "stream": True,
                    "messages": [{"role": "user", "content": "stream test"}],
                },
                timeout=10.0,
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :]
                    if data == "[DONE]":
                        done_seen = True
                        break
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"]
                    if content := delta.get("content"):
                        collected.append(content)

    assert collected == ["hello ", "world"]
    assert done_seen, "Expected [DONE] terminator"


# ---------------------------------------------------------------------------
# Test 5: scripted HTTP error
# ---------------------------------------------------------------------------


async def test_chat_completions_scripted_http_error_raises() -> None:
    """add_error enqueues an HTTP error response for the next chat request.

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:184
    """
    async with FauxLiteLLMServer() as srv:
        srv.add_error("smart", 502, "upstream down")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{srv.url}/v1/chat/completions",
                json={"model": "smart", "messages": [{"role": "user", "content": "q"}]},
            )

    assert resp.status_code == 502
    body = resp.json()
    assert "upstream down" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Test 6: deterministic embeddings
# ---------------------------------------------------------------------------


async def test_embeddings_returns_deterministic_vectors() -> None:
    """POST /v1/embeddings returns vectors of correct shape; same input → same vector.

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:230
    """
    async with FauxLiteLLMServer(dimension=128) as srv:
        async with httpx.AsyncClient() as client:
            resp1 = await client.post(
                f"{srv.url}/v1/embeddings",
                json={"model": "embed", "input": ["foo", "bar"]},
            )
            resp2 = await client.post(
                f"{srv.url}/v1/embeddings",
                json={"model": "embed", "input": ["foo", "bar"]},
            )

    assert resp1.status_code == 200
    body = resp1.json()
    assert body["object"] == "list"
    data = body["data"]
    assert len(data) == 2
    assert len(data[0]["embedding"]) == 128
    assert len(data[1]["embedding"]) == 128
    # Determinism: two calls with same input produce identical vectors
    assert body["data"] == resp2.json()["data"]
    # Different inputs produce different vectors
    assert data[0]["embedding"] != data[1]["embedding"]


# ---------------------------------------------------------------------------
# Test 7: empty queue → default content
# ---------------------------------------------------------------------------


async def test_queue_depletion_returns_empty_json_default() -> None:
    """When no response is scripted, the server returns '{}' as content (not a crash).

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:107
    """
    async with FauxLiteLLMServer() as srv:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{srv.url}/v1/chat/completions",
                json={"model": "smart", "messages": [{"role": "user", "content": "q"}]},
            )

    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert content == "{}"


# ---------------------------------------------------------------------------
# Test 8: reset clears all queues
# ---------------------------------------------------------------------------


async def test_reset_clears_all_queues() -> None:
    """reset() discards all enqueued entries; subsequent calls get the default.

    # Verified: libs/jarvis_common/jarvis_common/testing_sidecars/faux_litellm.py:192
    """
    async with FauxLiteLLMServer() as srv:
        srv.add_response("smart", '{"answer": "should not appear"}')
        srv.reset()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{srv.url}/v1/chat/completions",
                json={"model": "smart", "messages": [{"role": "user", "content": "q"}]},
            )

    content = resp.json()["choices"][0]["message"]["content"]
    assert content == "{}", f"Expected default '{{}}' after reset, got {content!r}"
