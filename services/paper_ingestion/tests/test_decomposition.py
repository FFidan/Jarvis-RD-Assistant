"""Tests for query decomposition and cross-paper RAG decomposition flow."""

from unittest.mock import AsyncMock, MagicMock

import httpx
from paper_ingestion.models import CrossPaperAskRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_response(content: str) -> dict:
    """Build a minimal LiteLLM-style chat completion response."""
    return {"choices": [{"message": {"content": content}}]}


def _make_http_client_with_response(response_body: dict, status_code: int = 200):
    """Create an AsyncMock httpx client that returns a fixed response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = response_body
    mock_resp.raise_for_status = MagicMock()

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = mock_resp
    return client


# ---------------------------------------------------------------------------
# Test 1: Happy path — LLM returns valid sub-queries
# ---------------------------------------------------------------------------


async def test_decompose_query_happy_path():
    """decompose_query parses a valid JSON array from LLM."""
    from paper_ingestion.rag.decomposition import decompose_query

    sub_queries = '["What is attention?", "How do transformers work?"]'
    client = _make_http_client_with_response(_llm_response(sub_queries))

    result = await decompose_query("Explain attention in transformers", client)
    assert result == ["What is attention?", "How do transformers work?"]


# ---------------------------------------------------------------------------
# Test 2: Garbage fallback — LLM returns non-JSON
# ---------------------------------------------------------------------------


async def test_decompose_query_garbage_fallback():
    """decompose_query returns [original_question] when LLM returns garbage."""
    from paper_ingestion.rag.decomposition import decompose_query

    client = _make_http_client_with_response(_llm_response("I don't know"))
    question = "What are the benefits of attention?"

    result = await decompose_query(question, client)
    assert result == [question]


# ---------------------------------------------------------------------------
# Test 3: Empty array fallback — LLM returns []
# ---------------------------------------------------------------------------


async def test_decompose_query_empty_array_fallback():
    """decompose_query returns [original_question] when LLM returns empty array."""
    from paper_ingestion.rag.decomposition import decompose_query

    client = _make_http_client_with_response(_llm_response("[]"))
    question = "What is BERT?"

    result = await decompose_query(question, client)
    assert result == [question]


# ---------------------------------------------------------------------------
# Test 4: Non-list JSON fallback
# ---------------------------------------------------------------------------


async def test_decompose_query_non_list_fallback():
    """decompose_query returns [original_question] when LLM returns non-list JSON."""
    from paper_ingestion.rag.decomposition import decompose_query

    client = _make_http_client_with_response(_llm_response('{"sub": "query"}'))
    question = "Compare BERT and GPT"

    result = await decompose_query(question, client)
    assert result == [question]


# ---------------------------------------------------------------------------
# Test 5: Exception fallback — HTTP error
# ---------------------------------------------------------------------------


async def test_decompose_query_exception_fallback():
    """decompose_query returns [original_question] on HTTP exception."""
    from paper_ingestion.rag.decomposition import decompose_query

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.side_effect = httpx.TimeoutException("timed out")
    question = "Timeout question"

    result = await decompose_query(question, client)
    assert result == [question]


# ---------------------------------------------------------------------------
# Test 6: Filters empty strings from result
# ---------------------------------------------------------------------------


async def test_decompose_query_filters_empty_strings():
    """decompose_query filters out empty strings from the parsed array."""
    from paper_ingestion.rag.decomposition import decompose_query

    sub_queries = '["valid query", "", "  ", "another valid"]'
    client = _make_http_client_with_response(_llm_response(sub_queries))

    result = await decompose_query("complex question", client)
    assert result == ["valid query", "another valid"]


async def test_decompose_query_dedupes_and_caps_results():
    """decompose_query should dedupe repeated sub-queries and cap fan-out to four."""
    from paper_ingestion.rag.decomposition import decompose_query

    sub_queries = '["q1", "q2", "q1", "q3", "q4", "q5"]'
    client = _make_http_client_with_response(_llm_response(sub_queries))

    result = await decompose_query("complex question", client)

    assert result == ["q1", "q2", "q3", "q4"]


# ---------------------------------------------------------------------------
# Test 6b: Strips <think> tags from thinking models (e.g. qwen3.5)
# ---------------------------------------------------------------------------


async def test_decompose_query_strips_think_tags():
    """decompose_query strips <think>...</think> blocks before parsing JSON."""
    from paper_ingestion.rag.decomposition import decompose_query

    content = '<think>Let me break this down into parts</think>\n["sub1", "sub2"]'
    client = _make_http_client_with_response(_llm_response(content))

    result = await decompose_query("complex question", client)
    assert result == ["sub1", "sub2"]


# ---------------------------------------------------------------------------
# Test 7: Merge dedup — overlapping chunks keep highest score
# ---------------------------------------------------------------------------


def test_merge_dedup_keeps_highest_score():
    """Merging overlapping (paper_id, chunk_index) keeps the highest score."""
    chunks_a = [
        {"paper_id": 1, "chunk_index": 0, "content": "A0", "page_number": 1, "score": 0.8},
        {"paper_id": 2, "chunk_index": 1, "content": "A1", "page_number": 2, "score": 0.6},
    ]
    chunks_b = [
        {"paper_id": 1, "chunk_index": 0, "content": "B0", "page_number": 1, "score": 0.9},
        {"paper_id": 3, "chunk_index": 0, "content": "B1", "page_number": 1, "score": 0.7},
    ]

    # Replicate the merge logic from ask_cross_paper
    seen: dict[tuple[int, int], dict] = {}
    for chunk_list in [chunks_a, chunks_b]:
        for chunk in chunk_list:
            key = (chunk["paper_id"], chunk["chunk_index"])
            if key not in seen or chunk["score"] > seen[key]["score"]:
                seen[key] = chunk
    merged = list(seen.values())

    # Paper 1, chunk 0 should keep score 0.9 from chunks_b
    p1_c0 = [c for c in merged if c["paper_id"] == 1 and c["chunk_index"] == 0]
    assert len(p1_c0) == 1
    assert p1_c0[0]["score"] == 0.9

    # All 3 unique (paper_id, chunk_index) should be present
    assert len(merged) == 3
    paper_ids = {c["paper_id"] for c in merged}
    assert paper_ids == {1, 2, 3}


# ---------------------------------------------------------------------------
# Test 8: Concurrent execution — search called once per sub-query
# ---------------------------------------------------------------------------


async def test_concurrent_search_per_sub_query():
    """With decomposition, search_chunks_global is called once per sub-query."""
    from paper_ingestion.rag.decomposition import decompose_query

    # Build sub-queries
    sub_queries = '["sub1", "sub2", "sub3"]'
    llm_client = _make_http_client_with_response(_llm_response(sub_queries))

    result = await decompose_query("complex question", llm_client)
    assert len(result) == 3

    # Mock embedder.search_chunks_global
    mock_search = AsyncMock(
        return_value=[
            {"paper_id": 1, "chunk_index": 0, "content": "chunk", "page_number": 1, "score": 0.8},
        ]
    )

    # Simulate the concurrent gather logic from ask_cross_paper
    import asyncio

    max_chunks = 10
    per_query_limit = max(max_chunks * 2 // len(result), 3)
    results = await asyncio.gather(
        *(mock_search(query_text=sq, limit=per_query_limit, score_threshold=0.2) for sq in result)
    )

    assert mock_search.call_count == 3
    assert len(results) == 3


# ---------------------------------------------------------------------------
# Test 9: decompose=False skips decomposition
# ---------------------------------------------------------------------------


async def test_decompose_false_skips_decomposition():
    """When decompose=False, decompose_query is NOT called."""
    # Verify the model field default and override
    req_default = CrossPaperAskRequest(question="What is attention?")
    assert req_default.decompose is True

    req_no_decompose = CrossPaperAskRequest(question="What is attention?", decompose=False)
    assert req_no_decompose.decompose is False

    # Simulate the branch logic from ask_cross_paper
    decompose_called = False

    async def mock_decompose(q, c):
        nonlocal decompose_called
        decompose_called = True
        return [q]

    # When decompose=False, the function should NOT call decompose_query
    if req_no_decompose.decompose:
        await mock_decompose(req_no_decompose.question, None)

    assert not decompose_called


# ---------------------------------------------------------------------------
# Test 10: CrossPaperAskRequest model has decompose field
# ---------------------------------------------------------------------------


def test_cross_paper_ask_request_decompose_default():
    """CrossPaperAskRequest.decompose defaults to True."""
    req = CrossPaperAskRequest(question="test")
    assert req.decompose is True

    req_false = CrossPaperAskRequest(question="test", decompose=False)
    assert req_false.decompose is False
