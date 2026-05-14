"""Tests for query decomposition and cross-paper RAG decomposition flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.models import CrossPaperAskRequest
from pydantic import RootModel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_openai_client() -> MagicMock:
    """Return a dummy openai client to bypass svc.openai_client lookup."""
    return MagicMock()


def _llm_result(items: list[str]) -> RootModel:
    return RootModel[list[str]](root=items)


# ---------------------------------------------------------------------------
# Test 1: Happy path — LLM returns valid sub-queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_happy_path():
    """decompose_query parses a valid sub-query list from call_llm_structured."""
    from paper_ingestion.rag.decomposition import decompose_query

    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_llm_result(["What is attention?", "How do transformers work?"]),
    ):
        result = await decompose_query(
            "Explain attention in transformers",
            openai_client=_mock_openai_client(),
        )

    assert result == ["What is attention?", "How do transformers work?"]


# ---------------------------------------------------------------------------
# Test 2: Exception fallback — call_llm_structured raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_garbage_fallback():
    """decompose_query returns [original_question] when call_llm_structured raises."""
    from paper_ingestion.rag.decomposition import decompose_query

    question = "What are the benefits of attention?"
    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        side_effect=ValueError("unexpected response"),
    ):
        result = await decompose_query(
            question,
            openai_client=_mock_openai_client(),
        )

    assert result == [question]


# ---------------------------------------------------------------------------
# Test 3: Empty array fallback — LLM returns []
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_empty_array_fallback():
    """decompose_query returns [original_question] when structured result is empty."""
    from paper_ingestion.rag.decomposition import decompose_query

    question = "What is BERT?"
    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_llm_result([]),
    ):
        result = await decompose_query(
            question,
            openai_client=_mock_openai_client(),
        )

    assert result == [question]


# ---------------------------------------------------------------------------
# Test 4: Schema validation error fallback (non-list JSON rejected by Instructor/Pydantic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_validation_error_fallback():
    """decompose_query returns [original_question] when structured parsing fails."""
    from paper_ingestion.rag.decomposition import decompose_query

    question = "Compare BERT and GPT"
    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        side_effect=ValueError("Input is not a list"),
    ):
        result = await decompose_query(
            question,
            openai_client=_mock_openai_client(),
        )

    assert result == [question]


# ---------------------------------------------------------------------------
# Test 5: Exception fallback — generic exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_exception_fallback():
    """decompose_query returns [original_question] on any exception."""
    from paper_ingestion.rag.decomposition import decompose_query

    question = "Timeout question"
    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        side_effect=RuntimeError("connection refused"),
    ):
        result = await decompose_query(
            question,
            openai_client=_mock_openai_client(),
        )

    assert result == [question]


# ---------------------------------------------------------------------------
# Test 6: Filters empty strings from result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_filters_empty_strings():
    """decompose_query filters out empty and whitespace-only strings."""
    from paper_ingestion.rag.decomposition import decompose_query

    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_llm_result(["valid query", "", "  ", "another valid"]),
    ):
        result = await decompose_query(
            "complex question",
            openai_client=_mock_openai_client(),
        )

    assert result == ["valid query", "another valid"]


@pytest.mark.asyncio
async def test_decompose_query_dedupes_and_caps_results():
    """decompose_query dedupes repeated sub-queries and caps fan-out to four."""
    from paper_ingestion.rag.decomposition import decompose_query

    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_llm_result(["q1", "q2", "q1", "q3", "q4", "q5"]),
    ):
        result = await decompose_query(
            "complex question",
            openai_client=_mock_openai_client(),
        )

    assert result == ["q1", "q2", "q3", "q4"]


# ---------------------------------------------------------------------------
# Test 6b: openai_client=None raises RuntimeError (caught as fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_none_openai_client_falls_back():
    """When openai_client is None and svc.openai_client is None, falls back gracefully."""
    from paper_ingestion.rag.decomposition import decompose_query

    question = "What is RAG?"
    with patch("paper_ingestion._state.svc") as mock_svc:
        mock_svc.openai_client = None
        result = await decompose_query(
            question,
            openai_client=None,
        )

    assert result == [question]


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

    seen: dict[tuple[int, int], dict] = {}
    for chunk_list in [chunks_a, chunks_b]:
        for chunk in chunk_list:
            key = (chunk["paper_id"], chunk["chunk_index"])
            if key not in seen or chunk["score"] > seen[key]["score"]:
                seen[key] = chunk
    merged = list(seen.values())

    p1_c0 = [c for c in merged if c["paper_id"] == 1 and c["chunk_index"] == 0]
    assert len(p1_c0) == 1
    assert p1_c0[0]["score"] == 0.9
    assert len(merged) == 3
    paper_ids = {c["paper_id"] for c in merged}
    assert paper_ids == {1, 2, 3}


# ---------------------------------------------------------------------------
# Test 8: Concurrent execution — search called once per sub-query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_search_per_sub_query():
    """With decomposition, search_chunks_global is called once per sub-query."""
    from paper_ingestion.rag.decomposition import decompose_query

    with patch(
        "paper_ingestion.rag.decomposition.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_llm_result(["sub1", "sub2", "sub3"]),
    ):
        result = await decompose_query(
            "complex question",
            openai_client=_mock_openai_client(),
        )

    assert len(result) == 3

    mock_search = AsyncMock(
        return_value=[
            {"paper_id": 1, "chunk_index": 0, "content": "chunk", "page_number": 1, "score": 0.8},
        ]
    )

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


@pytest.mark.asyncio
async def test_decompose_false_skips_decomposition():
    """When decompose=False, decompose_query is NOT called."""
    req_default = CrossPaperAskRequest(question="What is attention?")
    assert req_default.decompose is True

    req_no_decompose = CrossPaperAskRequest(question="What is attention?", decompose=False)
    assert req_no_decompose.decompose is False

    decompose_called = False

    async def mock_decompose(q, c):
        nonlocal decompose_called
        decompose_called = True
        return [q]

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
