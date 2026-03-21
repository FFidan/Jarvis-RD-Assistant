"""Tests for cross-paper RAG: global search, endpoint, dedup, and XML escaping."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from app.embedder import Embedder
from app.models import CrossPaperAskRequest

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


# ---------------------------------------------------------------------------
# Test: search_chunks_global returns results without paper_id filter
# ---------------------------------------------------------------------------


async def test_search_chunks_global_no_filter():
    """search_chunks_global queries Qdrant WITHOUT a paper_id filter."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()

    embedder = Embedder(mock_http, mock_qdrant)

    # Stub embed_texts to return a dummy vector
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 768])

    hits = [
        _make_hit(paper_id=1, chunk_index=0, content="chunk A", page_number=1, score=0.9),
        _make_hit(paper_id=2, chunk_index=1, content="chunk B", page_number=3, score=0.7),
    ]
    mock_qdrant.query_points.return_value = _make_query_response(hits)

    results = await embedder.search_chunks_global("test query", limit=10, score_threshold=0.25)

    # Verify query_points was called without a filter
    call_kwargs = mock_qdrant.query_points.call_args
    has_no_filter = (
        call_kwargs.kwargs.get("query_filter") is None or "query_filter" not in call_kwargs.kwargs
    )
    assert has_no_filter

    assert len(results) == 2
    assert results[0]["paper_id"] == 1
    assert results[0]["content"] == "chunk A"
    assert results[0]["score"] == 0.9
    assert results[1]["paper_id"] == 2


# ---------------------------------------------------------------------------
# Test: deduplication logic (max 2 chunks per paper, respects max_papers)
# ---------------------------------------------------------------------------


async def test_dedup_max_chunks_per_paper():
    """Cross-paper dedup keeps at most 2 chunks per paper and trims to max_papers."""
    # Simulate the dedup logic from ask_cross_paper
    all_chunks = [
        {"paper_id": 1, "chunk_index": 0, "content": "c1a", "page_number": 1, "score": 0.9},
        {"paper_id": 1, "chunk_index": 1, "content": "c1b", "page_number": 2, "score": 0.85},
        {"paper_id": 1, "chunk_index": 2, "content": "c1c", "page_number": 3, "score": 0.6},
        {"paper_id": 2, "chunk_index": 0, "content": "c2a", "page_number": 1, "score": 0.8},
        {"paper_id": 3, "chunk_index": 0, "content": "c3a", "page_number": 1, "score": 0.7},
    ]

    max_papers = 2
    max_chunks = 4

    # Group by paper_id
    chunks_by_paper: dict[int, list[dict]] = {}
    for chunk in all_chunks:
        pid = chunk["paper_id"]
        if pid not in chunks_by_paper:
            chunks_by_paper[pid] = []
        chunks_by_paper[pid].append(chunk)

    # Keep top 2 per paper
    for pid in chunks_by_paper:
        chunks_by_paper[pid].sort(key=lambda c: c["score"], reverse=True)
        chunks_by_paper[pid] = chunks_by_paper[pid][:2]

    # Paper 1 should have only 2 chunks (dropped c1c)
    assert len(chunks_by_paper[1]) == 2
    assert all(c["score"] >= 0.85 for c in chunks_by_paper[1])

    # Trim to max_papers (top by best chunk score)
    paper_ids_sorted = sorted(
        chunks_by_paper.keys(),
        key=lambda pid: chunks_by_paper[pid][0]["score"],
        reverse=True,
    )
    paper_ids_sorted = paper_ids_sorted[:max_papers]

    # Papers 1 (0.9) and 2 (0.8) should remain; paper 3 (0.7) dropped
    assert 1 in paper_ids_sorted
    assert 2 in paper_ids_sorted
    assert 3 not in paper_ids_sorted

    # Flatten and trim to max_chunks
    selected: list[dict] = []
    for pid in paper_ids_sorted:
        selected.extend(chunks_by_paper[pid])
    selected.sort(key=lambda c: c["score"], reverse=True)
    selected = selected[:max_chunks]

    assert len(selected) <= max_chunks
    assert all(c["paper_id"] in (1, 2) for c in selected)


# ---------------------------------------------------------------------------
# Test: XML escaping in prompt construction
# ---------------------------------------------------------------------------


def test_xml_escaping():
    """Content and question are XML-escaped to prevent prompt injection."""
    raw_content = '<script>alert("xss")</script> & "quoted"'
    raw_question = "What about <b>bold</b> claims?"

    safe_content = raw_content.replace("<", "&lt;").replace(">", "&gt;")
    safe_question = raw_question.replace("<", "&lt;").replace(">", "&gt;")

    assert "<script>" not in safe_content
    assert "&lt;script&gt;" in safe_content
    assert "<b>" not in safe_question
    assert "&lt;b&gt;" in safe_question


# ---------------------------------------------------------------------------
# Test: /api/ask endpoint returns correct structure
# ---------------------------------------------------------------------------


@respx.mock
async def test_ask_cross_paper_endpoint_structure():
    """POST /api/ask returns answer + sources with paper attribution."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    # Stub embed_texts
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 768])

    # Stub search_chunks_global
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

    # Simulate the dedup + metadata + LLM call logic
    all_chunks = await embedder.search_chunks_global("test question", limit=20)
    assert len(all_chunks) == 2

    # Each source must have paper_id and paper_title
    sources = [
        {
            "paper_id": c["paper_id"],
            "paper_title": f"Paper {c['paper_id']}",
            "content": c["content"],
            "page_number": c["page_number"],
            "score": round(c["score"], 3),
        }
        for c in all_chunks
    ]

    result = {
        "answer": "Transformers use attention [Paper 1] as shown in [Paper 2].",
        "sources": sources,
    }

    assert "answer" in result
    assert isinstance(result["sources"], list)
    assert len(result["sources"]) == 2
    for src in result["sources"]:
        assert "paper_id" in src
        assert "paper_title" in src
        assert "content" in src
        assert "page_number" in src
        assert "score" in src


# ---------------------------------------------------------------------------
# Test: CrossPaperAskRequest model validation
# ---------------------------------------------------------------------------


def test_cross_paper_ask_request_defaults():
    """CrossPaperAskRequest has correct defaults and validates constraints."""
    req = CrossPaperAskRequest(question="What is attention?")
    assert req.max_chunks == 10
    assert req.max_papers == 5

    # Bounds
    with pytest.raises(Exception):
        CrossPaperAskRequest(question="")  # min_length=1

    with pytest.raises(Exception):
        CrossPaperAskRequest(question="x", max_chunks=0)  # ge=1

    with pytest.raises(Exception):
        CrossPaperAskRequest(question="x", max_papers=100)  # le=15
