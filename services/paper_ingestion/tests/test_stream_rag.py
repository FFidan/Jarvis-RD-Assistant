"""Tests for streaming RAG endpoints and SSE event generation."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hit(paper_id: int, chunk_index: int, content: str, page_number: int, score: float):
    """Create a mock Qdrant ScoredPoint-like object."""
    hit = MagicMock()
    hit.payload = {
        "paper_id": paper_id,
        "chunk_index": chunk_index,
        "content": content,
        "page_number": page_number,
    }
    hit.score = score
    return hit


def _make_query_response(hits: list):
    """Wrap hits in a mock Qdrant query_points response."""
    resp = MagicMock()
    resp.points = hits
    return resp


class FakeSSELine:
    """Simulate an async line iterator for httpx streaming response."""

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aiter__(self):
        for line in self._lines:
            yield line


class FakeStreamResponse:
    """Simulate an httpx streaming response context manager."""

    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=MagicMock())

    def aiter_lines(self):
        return FakeSSELine(self._lines).__aiter__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeStreamResponseError:
    """Simulate an httpx streaming response that raises on status check."""

    def __init__(self, error: Exception):
        self._error = error

    def raise_for_status(self):
        raise self._error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Test 1: stream_rag_events yields correct SSE format
# ---------------------------------------------------------------------------


async def teststream_rag_events_format():
    """stream_rag_events yields token, sources, done, and [DONE] events."""
    from paper_ingestion.rag.streaming import stream_rag_events

    # Build fake SSE lines as LiteLLM would emit
    sse_lines = [
        'data: {"choices": [{"delta": {"content": "Hello"}}]}',
        'data: {"choices": [{"delta": {"content": " world"}}]}',
        "data: [DONE]",
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponse(sse_lines)

    messages = [{"role": "user", "content": "test prompt"}]
    sources = [{"content": "chunk text", "page_number": 1, "score": 0.9}]

    events = []
    async for event in stream_rag_events(mock_client, messages, sources):
        events.append(event)

    # Should have: token("Hello"), token(" world"), sources, done, [DONE]
    assert len(events) == 5

    # Check token events
    token1 = json.loads(events[0].replace("data: ", "").strip())
    assert token1["type"] == "token"
    assert token1["content"] == "Hello"

    token2 = json.loads(events[1].replace("data: ", "").strip())
    assert token2["type"] == "token"
    assert token2["content"] == " world"

    # Check sources event
    sources_event = json.loads(events[2].replace("data: ", "").strip())
    assert sources_event["type"] == "sources"
    assert len(sources_event["sources"]) == 1
    assert sources_event["sources"][0]["content"] == "chunk text"

    # Check done event
    done_event = json.loads(events[3].replace("data: ", "").strip())
    assert done_event["type"] == "done"
    assert done_event["full_answer"] == "Hello world"

    # Check [DONE] sentinel
    assert events[4].strip() == "data: [DONE]"


# ---------------------------------------------------------------------------
# Test 2: Token parsing — verify tokens extracted correctly from chunks
# ---------------------------------------------------------------------------


async def teststream_rag_events_token_parsing():
    """Tokens are correctly extracted from LiteLLM delta chunks."""
    from paper_ingestion.rag.streaming import stream_rag_events

    # Simulate chunks with empty deltas (e.g. role-only first chunk)
    sse_lines = [
        'data: {"choices": [{"delta": {"role": "assistant"}}]}',
        'data: {"choices": [{"delta": {"content": "The"}}]}',
        'data: {"choices": [{"delta": {"content": " answer"}}]}',
        'data: {"choices": [{"delta": {"content": " is"}}]}',
        'data: {"choices": [{"delta": {}}]}',  # empty delta
        'data: {"choices": [{"delta": {"content": " 42."}}]}',
        "",  # blank line (should be skipped)
        "data: [DONE]",
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponse(sse_lines)

    messages = [{"role": "user", "content": "question"}]
    sources = []

    tokens = []
    async for event in stream_rag_events(mock_client, messages, sources):
        data_str = event.replace("data: ", "").strip()
        if not data_str or data_str == "[DONE]":
            continue
        parsed = json.loads(data_str)
        if parsed.get("type") == "token":
            tokens.append(parsed["content"])
        elif parsed.get("type") == "done":
            assert parsed["full_answer"] == "The answer is 42."

    assert tokens == ["The", " answer", " is", " 42."]


# ---------------------------------------------------------------------------
# Test 3: Error handling — error event yielded on stream failure
# ---------------------------------------------------------------------------


async def teststream_rag_events_error_handling():
    """An error event is yielded when the LiteLLM stream fails."""
    from paper_ingestion.rag.streaming import stream_rag_events

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponseError(
        httpx.HTTPStatusError("502 Bad Gateway", request=MagicMock(), response=MagicMock())
    )

    messages = [{"role": "user", "content": "question"}]
    sources = [{"content": "c1", "page_number": 1, "score": 0.8}]

    events = []
    async for event in stream_rag_events(mock_client, messages, sources):
        events.append(event)

    # Should yield error event + [DONE] terminator, no sources or done
    assert len(events) == 2
    error_event = json.loads(events[0].replace("data: ", "").strip())
    assert error_event["type"] == "error"
    # Error message should be user-friendly (sanitized), not raw exception text
    assert "An error occurred" in error_event["message"]
    assert "502 Bad Gateway" not in error_event["message"]
    assert events[1].strip() == "data: [DONE]"


async def teststream_rag_events_uses_shared_litellm_config_base_url(monkeypatch):
    """Streaming RAG should use the shared LiteLLM base URL (transparent proxy, no auth)."""
    from paper_ingestion.rag.streaming import stream_rag_events

    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponse(["data: [DONE]"])

    events = []
    async for event in stream_rag_events(
        mock_client,
        [{"role": "user", "content": "question"}],
        [],
    ):
        events.append(event)

    assert events == [
        'data: {"type": "sources", "sources": []}\n\n',
        'data: {"type": "done", "full_answer": "", "model_used": null}\n\n',
        "data: [DONE]\n\n",
    ]
    mock_client.stream.assert_called_once_with(
        "POST",
        "http://litellm.test:4000/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "question"}],
            "stream": True,
            "temperature": 0.1,
            "max_tokens": 700,
        },
        headers={},
        timeout=300.0,
    )


# ---------------------------------------------------------------------------
# Test 4: Non-streaming endpoints still work (backward compat)
# ---------------------------------------------------------------------------


async def testprepare_single_paper_rag_returns_messages_and_sources():
    """prepare_single_paper_rag returns (messages, sources_list) tuple."""
    from paper_ingestion.embedder import Embedder
    from paper_ingestion.models import AskRequest
    from paper_ingestion.rag.streaming import prepare_single_paper_rag

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    # Stub embed_texts and search
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])

    hits = [
        _make_hit(paper_id=1, chunk_index=0, content="chunk A", page_number=1, score=0.9),
        _make_hit(paper_id=1, chunk_index=1, content="chunk B", page_number=2, score=0.8),
    ]
    mock_qdrant.query_points.return_value = _make_query_response(hits)

    # Stub rerank to pass through
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, chunks, top_k: chunks[:top_k])

    # Mock db_pool with proper async context manager for acquire()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Test Paper"}

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_conn
    mock_cm.__aexit__.return_value = False

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_cm

    body = AskRequest(question="What is the main finding?", max_chunks=5)

    messages, sources = await prepare_single_paper_rag(
        embedder, mock_pool, paper_id=1, body=body, http_client=mock_http
    )

    # Messages should be a list with a single user message
    assert isinstance(messages, list)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "Test Paper" in messages[0]["content"]
    assert "What is the main finding" in messages[0]["content"]

    # Sources should contain the chunks
    assert isinstance(sources, list)
    assert len(sources) == 2
    assert sources[0]["content"] == "chunk A"
    assert sources[0]["page_number"] == 1
    assert sources[0]["score"] == 0.9


# ---------------------------------------------------------------------------
# Test 5: prepare_cross_paper_rag returns messages and sources
# ---------------------------------------------------------------------------


async def testprepare_cross_paper_rag_returns_messages_and_sources():
    """prepare_cross_paper_rag returns (messages, sources) with paper attribution."""
    from paper_ingestion.embedder import Embedder
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import prepare_cross_paper_rag

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    embedder.search_chunks_global = AsyncMock(
        return_value=[
            {
                "paper_id": 10,
                "chunk_index": 0,
                "content": "Finding about transformers.",
                "page_number": 2,
                "score": 0.88,
            },
            {
                "paper_id": 20,
                "chunk_index": 1,
                "content": "Evidence about attention.",
                "page_number": 5,
                "score": 0.75,
            },
        ]
    )
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, chunks, top_k: chunks[:top_k])

    # Mock db_pool with proper async context manager for acquire()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"id": 10, "title": "Transformer Paper", "authors": "A", "url": "http://a"},
        {"id": 20, "title": "Attention Paper", "authors": "B", "url": "http://b"},
    ]

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_conn
    mock_cm.__aexit__.return_value = False

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_cm

    body = CrossPaperAskRequest(question="How do transformers work?", decompose=False)

    result = await prepare_cross_paper_rag(embedder, mock_pool, body, mock_http)

    # Should return a CrossPaperRagPrep dataclass (since chunks were found)
    from paper_ingestion.rag.streaming import CrossPaperRagPrep

    assert isinstance(result, CrossPaperRagPrep)
    messages = result.messages
    sources = result.sources

    assert isinstance(messages, list)
    assert len(messages) == 1
    assert "Transformer Paper" in messages[0]["content"]
    assert "Attention Paper" in messages[0]["content"]

    assert isinstance(sources, list)
    assert len(sources) == 2
    assert sources[0]["paper_id"] == 10
    assert sources[0]["paper_title"] == "Transformer Paper"


# ---------------------------------------------------------------------------
# Test 6: prepare_cross_paper_rag returns dict when no chunks found
# ---------------------------------------------------------------------------


async def testprepare_cross_paper_rag_no_chunks_returns_dict():
    """When no chunks match, prepare_cross_paper_rag returns a canned dict."""
    from paper_ingestion.embedder import Embedder
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import prepare_cross_paper_rag

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    mock_pool = AsyncMock()

    body = CrossPaperAskRequest(question="Something irrelevant", decompose=False)

    result = await prepare_cross_paper_rag(embedder, mock_pool, body, mock_http)

    # Returns CrossPaperRagNoResults dataclass when no chunks found
    from paper_ingestion.rag.streaming import CrossPaperRagNoResults

    assert isinstance(result, CrossPaperRagNoResults)
    assert "No relevant information" in result.answer
    assert result.sources == []


# ---------------------------------------------------------------------------
# Test 7: SSE events have correct double-newline termination
# ---------------------------------------------------------------------------


async def teststream_rag_events_sse_termination():
    """Each SSE event line ends with double newline for proper SSE format."""
    from paper_ingestion.rag.streaming import stream_rag_events

    sse_lines = [
        'data: {"choices": [{"delta": {"content": "ok"}}]}',
        "data: [DONE]",
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponse(sse_lines)

    events = []
    async for event in stream_rag_events(mock_client, [{"role": "user", "content": "q"}], []):
        events.append(event)

    # Every event should end with \n\n
    for event in events:
        assert event.endswith("\n\n"), f"Event does not end with double newline: {event!r}"

    # Every event should start with "data: "
    for event in events:
        assert event.startswith("data: "), f"Event does not start with 'data: ': {event!r}"


# ---------------------------------------------------------------------------
# Test 8: confidence event emitted between done and [DONE] when verifier provided
# ---------------------------------------------------------------------------


async def teststream_rag_events_confidence_event_emitted_before_done():
    """confidence SSE event appears after done and before [DONE] when verifier+pool provided."""
    import json
    from unittest.mock import MagicMock

    from paper_ingestion.rag.streaming import stream_rag_events

    sse_lines = [
        'data: {"choices": [{"delta": {"content": "Neural ODEs are powerful."}}]}',
        "data: [DONE]",
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = FakeStreamResponse(sse_lines)

    sources = [{"content": "Neural ODEs are powerful.", "page_number": 1}]

    # Build a stub verifier that always verifies
    _vresult = MagicMock()
    _vresult.verified = True
    _vresult.match_type = "exact"
    _vresult.match_score = 1.0
    stub_verifier = MagicMock()
    stub_verifier.verify_quote.return_value = _vresult

    # Build a stub pool (single-paper path: no paper_id in sources → no DB fetch)
    stub_pool = MagicMock()

    valid_confidence = {"HIGH", "MEDIUM", "LOW", "UNVERIFIED"}

    events: list[str] = []
    async for event in stream_rag_events(
        mock_client,
        [{"role": "user", "content": "What are neural ODEs?"}],
        sources,
        verifier=stub_verifier,
        db_pool=stub_pool,
    ):
        events.append(event)

    # Parse all data events (skip [DONE] sentinel)
    parsed: list[dict] = []
    for ev in events:
        data_str = ev.replace("data: ", "", 1).strip()
        if data_str == "[DONE]":
            continue
        parsed.append(json.loads(data_str))

    event_types = [e["type"] for e in parsed]

    # Sequence: token → sources → done → confidence
    assert "token" in event_types
    assert event_types.index("sources") > event_types.index("token")
    assert event_types.index("done") > event_types.index("sources")
    assert event_types.index("confidence") > event_types.index("done")

    # Last raw event is the [DONE] sentinel
    assert events[-1].strip() == "data: [DONE]"

    # Validate confidence event payload
    conf_event = next(e for e in parsed if e["type"] == "confidence")
    assert set(conf_event.keys()) >= {"type", "confidence", "verified_fraction", "per_sentence"}
    assert conf_event["confidence"] in valid_confidence
    assert isinstance(conf_event["verified_fraction"], float)
    assert isinstance(conf_event["per_sentence"], list)
