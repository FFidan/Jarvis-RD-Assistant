"""Tests for error handling in SSE streaming, embedder, and health checks."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from paper_ingestion.ingestion.embedder import Embedder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeStreamResponseError:
    """Simulate an httpx streaming response that raises on enter/status check."""

    def __init__(self, error: Exception):
        self._error = error

    def raise_for_status(self):
        raise self._error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Test 1: Cross-paper stream returns SSE error on preparation failure
# ---------------------------------------------------------------------------


async def test_stream_cross_paper_rag_preparation_error():
    """When _prepare_cross_paper_rag raises, _sse_error_stream yields SSE error events."""
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import prepare_cross_paper_rag, sse_error_stream

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    # Make search_chunks_global raise a RuntimeError (embedding service down)
    embedder.search_chunks_global = AsyncMock(
        side_effect=RuntimeError("Embedding service unavailable")
    )

    mock_pool = AsyncMock()

    body = CrossPaperAskRequest(question="What is attention?", decompose=False)

    # The prepare_cross_paper_rag should raise when search fails
    with pytest.raises(RuntimeError):
        await prepare_cross_paper_rag(embedder, mock_pool, body, mock_http)

    # Verify the sse_error_stream helper yields correct SSE events
    events = []
    async for event in sse_error_stream(
        "An error occurred while preparing the response. Please try again."
    ):
        events.append(event)

    assert len(events) == 2
    error_event = json.loads(events[0].replace("data: ", "").strip())
    assert error_event["type"] == "error"
    assert "An error occurred" in error_event["message"]
    assert events[1].strip() == "data: [DONE]"


# ---------------------------------------------------------------------------
# Test 2: SSE error messages are sanitized (no raw exception text)
# ---------------------------------------------------------------------------


async def test_stream_rag_sanitized_error():
    """SSE error events contain user-friendly messages, not raw exception text."""
    from paper_ingestion.rag.streaming import stream_rag_events

    # Test with a generic exception -- should get sanitized message
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponseError(
        ValueError("Internal: DB connection string leak jdbc://user:pass@host")
    )

    messages = [{"role": "user", "content": "question"}]
    sources = [{"content": "c1", "page_number": 1, "score": 0.8}]

    events = []
    async for event in stream_rag_events(mock_client, messages, sources):
        events.append(event)

    assert len(events) == 2
    error_event = json.loads(events[0].replace("data: ", "").strip())
    assert error_event["type"] == "error"
    # Should NOT contain the raw exception text
    assert "jdbc://" not in error_event["message"]
    assert "DB connection" not in error_event["message"]
    # Should contain the user-friendly message
    assert "An error occurred while generating the response" in error_event["message"]


async def test_stream_rag_timeout_error_sanitized():
    """SSE error for TimeoutException shows user-friendly timeout message."""
    from paper_ingestion.rag.streaming import stream_rag_events

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponseError(
        httpx.TimeoutException("pool timeout on POST http://litellm:4000/v1/...")
    )

    events = []
    async for event in stream_rag_events(mock_client, [{"role": "user", "content": "q"}], []):
        events.append(event)

    error_event = json.loads(events[0].replace("data: ", "").strip())
    assert error_event["type"] == "error"
    assert "timed out" in error_event["message"]
    assert "pool timeout" not in error_event["message"]


async def test_stream_rag_connect_error_sanitized():
    """SSE error for ConnectError shows user-friendly connection message."""
    from paper_ingestion.rag.streaming import stream_rag_events

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponseError(
        httpx.ConnectError("Connection refused: http://litellm:4000")
    )

    events = []
    async for event in stream_rag_events(mock_client, [{"role": "user", "content": "q"}], []):
        events.append(event)

    error_event = json.loads(events[0].replace("data: ", "").strip())
    assert error_event["type"] == "error"
    assert "Cannot connect to LLM service" in error_event["message"]
    assert "Connection refused" not in error_event["message"]


# ---------------------------------------------------------------------------
# Test 3: embed_texts raises RuntimeError on timeout
# ---------------------------------------------------------------------------


async def test_embed_texts_timeout():
    """embed_texts wraps httpx.TimeoutException as RuntimeError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    mock_http.post.side_effect = httpx.TimeoutException("read timed out")

    with pytest.raises(RuntimeError, match="Embedding service timed out"):
        await embedder.embed_texts(["test text"])


# ---------------------------------------------------------------------------
# Test 4: embed_texts raises RuntimeError on connection error
# ---------------------------------------------------------------------------


async def test_embed_texts_connection_error():
    """embed_texts wraps httpx.ConnectError as RuntimeError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    mock_http.post.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(RuntimeError, match="Embedding service unavailable"):
        await embedder.embed_texts(["test text"])


# ---------------------------------------------------------------------------
# Test 5: search_chunks returns empty list on Qdrant failure
# ---------------------------------------------------------------------------


async def test_search_chunks_qdrant_failure_returns_empty():
    """search_chunks_in_paper returns [] when Qdrant raises an exception."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    # embed_texts succeeds
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])

    # Qdrant query_points fails with a connection error
    mock_qdrant.query_points.side_effect = ConnectionError("Qdrant connection lost")

    result = await embedder.search_chunks_in_paper(
        query_text="test query",
        paper_id=1,
        limit=5,
        score_threshold=0.3,
    )

    assert result == []


async def test_search_chunks_global_qdrant_failure_returns_empty():
    """search_chunks_global returns [] when Qdrant raises an exception."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    mock_qdrant.query_points.side_effect = ConnectionError("Qdrant connection lost")

    result = await embedder.search_chunks_global(
        query_text="test query",
        limit=10,
        score_threshold=0.2,
    )

    assert result == []


async def test_search_chunks_runtime_error_propagates():
    """RuntimeError from embed_texts propagates through search_chunks_in_paper."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(side_effect=RuntimeError("Embedding service timed out"))

    with pytest.raises(RuntimeError, match="Embedding service timed out"):
        await embedder.search_chunks_in_paper(
            query_text="test query",
            paper_id=1,
            limit=5,
        )


# test_health_check_degraded and test_health_check_all_ok deleted —
# covered by libs/jarvis_common/tests/contract/test_health_contract.py
# (test_health_internal_503_when_db_down, test_health_internal_200_full_payload).
