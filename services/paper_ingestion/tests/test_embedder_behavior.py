"""Characterization tests for Embedder — safety net before ingestion/ split (C3).

Every public method of Embedder is covered with smoke/snapshot tests that mock
all external I/O (httpx, qdrant_client).  These tests exist to catch regressions
during the structural refactor; they are NOT integration tests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.embedder import COLLECTION_NAME, EMBEDDING_DIMENSION, Embedder
from paper_ingestion.models import ChunkForEmbedding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEncoding:
    """Character-level tiktoken stand-in so tests don't need the real model."""

    def encode(self, text: str) -> list[str]:
        return list(text)

    def decode(self, tokens: list[str]) -> str:
        return "".join(tokens)


def _make_embedder() -> Embedder:
    http = AsyncMock()
    qdrant = AsyncMock()
    e = Embedder(http, qdrant)
    e._encoding = _FakeEncoding()
    return e


def _embed_response(n: int = 1, dim: int = EMBEDDING_DIMENSION) -> MagicMock:
    """Build a fake httpx response for embed_texts."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": [{"index": i, "embedding": [0.1] * dim} for i in range(n)]}
    return resp


def _chunk(index: int = 0, content: str = "hello world") -> ChunkForEmbedding:
    return ChunkForEmbedding(
        chunk_index=index,
        content=content,
        page_number=1,
        start_char=0,
        end_char=len(content),
    )


def _qdrant_hit(paper_id: int = 1, score: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        payload={
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": "chunk content here",
            "page_number": 1,
        },
        score=score,
    )


# ---------------------------------------------------------------------------
# embed_texts
# ---------------------------------------------------------------------------


async def test_embed_texts_empty_list():
    """embed_texts([]) returns an empty list without making any HTTP call."""
    e = _make_embedder()
    result = await e.embed_texts([])
    assert result == []
    e.http_client.post.assert_not_awaited()


async def test_embed_texts_returns_embeddings(monkeypatch):
    """embed_texts posts to LiteLLM and returns ordered embedding vectors."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

    e = _make_embedder()
    e.http_client.post.return_value = _embed_response(2)

    result = await e.embed_texts(["text one", "text two"])

    assert len(result) == 2
    assert all(len(v) == EMBEDDING_DIMENSION for v in result)
    e.http_client.post.assert_awaited_once()


async def test_embed_texts_dimension_mismatch_raises(monkeypatch):
    """embed_texts raises ValueError when returned embedding dim doesn't match config."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    e = _make_embedder()
    # Return dim=5, but EMBEDDING_DIMENSION is 768
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * 5}]}
    e.http_client.post.return_value = resp

    with pytest.raises(ValueError, match="Embedding dimension mismatch"):
        await e.embed_texts(["text"])


async def test_embed_texts_timeout_raises(monkeypatch):
    """embed_texts re-raises TimeoutException as RuntimeError."""
    import httpx

    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    e = _make_embedder()
    e.http_client.post.side_effect = httpx.TimeoutException("timed out")

    with pytest.raises(RuntimeError, match="timed out"):
        await e.embed_texts(["text"])


async def test_embed_texts_connect_error_raises(monkeypatch):
    """embed_texts re-raises ConnectError as RuntimeError."""
    import httpx

    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    e = _make_embedder()
    e.http_client.post.side_effect = httpx.ConnectError("refused")

    with pytest.raises(RuntimeError, match="unavailable"):
        await e.embed_texts(["text"])


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


async def test_chunk_text_short_text_is_single_chunk():
    """A very short text produces exactly one chunk."""
    e = _make_embedder()
    chunks = e.chunk_text("Hello world.")
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].content == "Hello world."


async def test_chunk_text_respects_token_limit():
    """chunk_text splits text that exceeds CHUNK_TOKEN_LIMIT tokens."""
    from paper_ingestion.embedder import CHUNK_TOKEN_LIMIT

    e = _make_embedder()
    # Each char = 1 token via _FakeEncoding; repeat 'A' * (3 * CHUNK_TOKEN_LIMIT)
    long_text = "A" * (3 * CHUNK_TOKEN_LIMIT)
    chunks = e.chunk_text(long_text)
    # With character-level encoding every chunk must stay within the limit
    for chunk in chunks:
        assert len(chunk.content) <= CHUNK_TOKEN_LIMIT


async def test_chunk_text_page_boundaries_assigned():
    """Chunks get correct page numbers when page_boundaries are supplied.

    With _FakeEncoding (1 char = 1 token) and CHUNK_TOKEN_LIMIT=512, a section
    of 400 chars fits in one chunk.  The chunk's page_number is based on the
    mid-point of the chunk text.  We verify that chunk_text does assign a
    page_number (i.e., it's not None) when boundaries are provided.
    """
    e = _make_embedder()
    # Two sections: page1 is large (600 chars) to force a chunk split, page2 smaller
    # CHUNK_TOKEN_LIMIT=512, so page1 alone exceeds it and must be sub-split
    from paper_ingestion.embedder import CHUNK_TOKEN_LIMIT

    page1 = "A" * (CHUNK_TOKEN_LIMIT + 100)  # 612 chars — exceeds limit, will be split
    page2 = "B" * 100
    text = page1 + "\n\n" + page2
    page1_end = len(page1)
    boundaries = [(0, page1_end), (page1_end, len(text))]

    chunks = e.chunk_text(text, page_boundaries=boundaries)
    assert len(chunks) >= 1
    # All chunks should have non-None page_number when boundaries are provided
    for chunk in chunks:
        assert chunk.page_number is not None
    # The first chunk must be from page1 region (page_number=1)
    assert chunks[0].page_number == 1


async def test_chunk_text_no_page_boundaries_gives_none():
    """Chunks have page_number=None when no page_boundaries are given."""
    e = _make_embedder()
    chunks = e.chunk_text("short text")
    assert chunks[0].page_number is None


async def test_chunk_text_indexes_are_sequential():
    """chunk_index values form a contiguous 0-based sequence."""

    e = _make_embedder()
    long_text = ("word " * 200 + "\n\n") * 5  # multiple paragraphs, exceeds limit
    chunks = e.chunk_text(long_text)
    for expected_idx, chunk in enumerate(chunks):
        assert chunk.chunk_index == expected_idx


# ---------------------------------------------------------------------------
# embed_and_store (the "embed_and_upsert" method per the task brief)
# ---------------------------------------------------------------------------


async def test_embed_and_store_returns_uuids(monkeypatch):
    """embed_and_store returns one UUID per chunk and calls qdrant.upsert."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.1] * EMBEDDING_DIMENSION] * 2)

    chunks = [_chunk(0, "chunk A"), _chunk(1, "chunk B")]
    point_ids = await e.embed_and_store(paper_id=42, chunks=chunks)

    assert len(point_ids) == 2
    for pid in point_ids:
        uuid.UUID(pid)  # must be valid UUID
    e.qdrant.upsert.assert_awaited_once()


async def test_embed_and_store_empty_chunks():
    """embed_and_store with no chunks returns empty list and skips qdrant."""
    e = _make_embedder()
    result = await e.embed_and_store(paper_id=1, chunks=[])
    assert result == []
    e.qdrant.upsert.assert_not_awaited()


async def test_embed_and_store_cleanup_on_partial_failure(monkeypatch):
    """embed_and_store rolls back already-upserted points when a later batch fails."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    e = _make_embedder()

    call_count = 0

    async def _flaky_embed(texts):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [[0.1] * EMBEDDING_DIMENSION] * len(texts)
        raise RuntimeError("embed service down")

    e.embed_texts = _flaky_embed

    # Two batches: batch_size=1 forces two embed_texts calls
    chunks = [_chunk(0, "A"), _chunk(1, "B")]
    with pytest.raises(RuntimeError):
        await e.embed_and_store(paper_id=5, chunks=chunks, batch_size=1)

    # The first batch was upserted; cleanup (delete) must have been called
    e.qdrant.delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# search_similar
# ---------------------------------------------------------------------------


async def test_search_similar_returns_results():
    """search_similar calls query_points and formats results correctly."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    e.qdrant.query_points.return_value = SimpleNamespace(points=[_qdrant_hit(paper_id=7)])

    results = await e.search_similar("query text", limit=5)

    assert len(results) == 1
    assert results[0]["paper_id"] == 7
    assert results[0]["score"] == 0.9


async def test_search_similar_skips_null_payload():
    """search_similar ignores Qdrant hits with None payload."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    null_hit = SimpleNamespace(payload=None, score=0.99)
    good_hit = _qdrant_hit(paper_id=3)
    e.qdrant.query_points.return_value = SimpleNamespace(points=[null_hit, good_hit])

    results = await e.search_similar("query")
    assert len(results) == 1
    assert results[0]["paper_id"] == 3


async def test_search_similar_content_truncated_to_200():
    """search_similar truncates content to 200 chars."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    long_content_hit = SimpleNamespace(
        payload={"paper_id": 1, "chunk_index": 0, "content": "X" * 300, "page_number": 1},
        score=0.8,
    )
    e.qdrant.query_points.return_value = SimpleNamespace(points=[long_content_hit])

    results = await e.search_similar("q")
    assert len(results[0]["content"]) == 200


# ---------------------------------------------------------------------------
# search_chunks_in_paper
# ---------------------------------------------------------------------------


async def test_search_chunks_in_paper_returns_results():
    """search_chunks_in_paper filters by paper_id and returns chunks."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    e.qdrant.query_points.return_value = SimpleNamespace(points=[_qdrant_hit(paper_id=10)])

    results = await e.search_chunks_in_paper("query", paper_id=10, limit=5)

    assert len(results) == 1
    assert "content" in results[0]
    assert "score" in results[0]


async def test_search_chunks_in_paper_qdrant_error_returns_empty():
    """search_chunks_in_paper returns [] when Qdrant raises a generic error."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    e.qdrant.query_points.side_effect = Exception("connection refused")

    results = await e.search_chunks_in_paper("query", paper_id=1)
    assert results == []


async def test_search_chunks_in_paper_propagates_runtime_error():
    """search_chunks_in_paper does NOT catch RuntimeError from embed_texts."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(side_effect=RuntimeError("embed down"))

    with pytest.raises(RuntimeError):
        await e.search_chunks_in_paper("query", paper_id=1)


# ---------------------------------------------------------------------------
# search_chunks_global
# ---------------------------------------------------------------------------


async def test_search_chunks_global_returns_results():
    """search_chunks_global searches without a paper_id filter."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    e.qdrant.query_points.return_value = SimpleNamespace(
        points=[_qdrant_hit(paper_id=5), _qdrant_hit(paper_id=6, score=0.7)]
    )

    results = await e.search_chunks_global("neural networks", limit=10)

    assert len(results) == 2
    assert results[0]["paper_id"] == 5


async def test_search_chunks_global_empty_on_qdrant_error():
    """search_chunks_global degrades to [] on Qdrant exceptions."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    e.qdrant.query_points.side_effect = Exception("qdrant down")

    results = await e.search_chunks_global("query")
    assert results == []


async def test_search_chunks_global_clamps_limit():
    """search_chunks_global respects the max limit of 200."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    e.qdrant.query_points.return_value = SimpleNamespace(points=[])

    await e.search_chunks_global("query", limit=9999)
    call_kwargs = e.qdrant.query_points.call_args
    assert (
        call_kwargs.kwargs.get("limit", call_kwargs.args[1] if len(call_kwargs.args) > 1 else 200)
        <= 200
    )


# ---------------------------------------------------------------------------
# hybrid_search
# ---------------------------------------------------------------------------


async def test_hybrid_search_combines_bm25_and_semantic():
    """hybrid_search merges BM25 and semantic results via RRF."""
    from unittest.mock import MagicMock

    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])

    # Fake BM25 result from Postgres
    bm25_row = MagicMock()
    bm25_row.__getitem__ = lambda s, k: {
        "id": 1,
        "title": "BM25 Paper",
        "authors": ["Alice"],
        "url": "http://example.com",
        "abstract": "test abstract",
        "published_date": None,
    }[k]

    # Fake db pool
    conn = AsyncMock()
    conn.fetch.return_value = [bm25_row]
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db_pool = MagicMock()
    db_pool.acquire.return_value = ctx

    # search_chunks_global returns one chunk for paper_id=1
    e.search_chunks_global = AsyncMock(
        return_value=[{"paper_id": 1, "score": 0.8, "content": "c", "chunk_index": 0}]
    )

    results = await e.hybrid_search("test query", db_pool=db_pool, limit=5)

    assert isinstance(results, list)
    # Paper 1 should appear (it was in both BM25 and semantic)
    ids = [r["id"] for r in results]
    assert 1 in ids


async def test_hybrid_search_returns_list_on_no_results():
    """hybrid_search returns [] when no papers match either leg."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])
    e.search_chunks_global = AsyncMock(return_value=[])

    conn = AsyncMock()
    conn.fetch.return_value = []
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db_pool = MagicMock()
    db_pool.acquire.return_value = ctx

    results = await e.hybrid_search("nothing matches", db_pool=db_pool)
    assert results == []


# ---------------------------------------------------------------------------
# discover_from_seeds
# ---------------------------------------------------------------------------


async def test_discover_from_seeds_deduplicates_by_paper_id():
    """discover_from_seeds keeps the best score per paper_id."""
    e = _make_embedder()
    e.qdrant.scroll.return_value = ([SimpleNamespace(id="pt-1")], None)
    e.qdrant.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(payload={"paper_id": 99, "content": "low"}, score=0.5),
            SimpleNamespace(payload={"paper_id": 99, "content": "high"}, score=0.9),
            SimpleNamespace(payload=None, score=0.99),  # null payload — must be skipped
        ]
    )
    db_pool = MagicMock()

    results = await e.discover_from_seeds([1], db_pool=db_pool, limit=5)

    assert len(results) == 1
    assert results[0]["paper_id"] == 99
    assert results[0]["score"] == 0.9


async def test_discover_from_seeds_no_seeds_returns_empty():
    """discover_from_seeds with empty seed list returns []."""
    e = _make_embedder()
    db_pool = MagicMock()
    results = await e.discover_from_seeds([], db_pool=db_pool)
    assert results == []
    e.qdrant.scroll.assert_not_awaited()


async def test_discover_from_seeds_fallback_when_no_qdrant_points(monkeypatch):
    """When a seed has no Qdrant points, embed title+abstract as fallback."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    e = _make_embedder()
    # No scroll results — triggers DB fallback
    e.qdrant.scroll.return_value = ([], None)

    conn = AsyncMock()
    conn.fetchrow.return_value = {"title": "Paper Title", "abstract": "An abstract."}
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db_pool = MagicMock()
    db_pool.acquire.return_value = ctx

    e.embed_texts = AsyncMock(return_value=[[0.1] * EMBEDDING_DIMENSION])
    e.qdrant.query_points.return_value = SimpleNamespace(points=[])

    results = await e.discover_from_seeds([42], db_pool=db_pool, limit=5)
    # embed_texts must have been called (for title+abstract fallback)
    e.embed_texts.assert_awaited()
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# rerank_chunks
# ---------------------------------------------------------------------------


async def test_rerank_chunks_returns_top_k_when_no_reranker():
    """rerank_chunks falls back to top_k slice when reranker is None.

    get_reranker is imported inside rerank_chunks from paper_ingestion.reranker
    (the back-compat shim), so we patch at that module location.
    """
    e = _make_embedder()
    chunks = [{"content": f"chunk {i}", "score": 1.0 - i * 0.1} for i in range(10)]

    with patch("paper_ingestion.reranker.get_reranker", return_value=None):
        result = await e.rerank_chunks("query", chunks, top_k=3)

    assert result == chunks[:3]


async def test_rerank_chunks_uses_reranker_when_available():
    """rerank_chunks delegates to the reranker and returns re-ordered chunks.

    rerank_chunks skips the reranker when len(chunks) <= top_k, so we must
    supply more chunks than top_k to trigger the reranker path.
    ingestion/embedder.py imports get_reranker from paper_ingestion.reranker
    (the back-compat shim), so we patch there.
    """
    e = _make_embedder()
    # 5 chunks, top_k=2 → reranker is invoked (5 > 2)
    chunks = [
        {"content": "A"},
        {"content": "B"},
        {"content": "C"},
        {"content": "D"},
        {"content": "E"},
    ]

    mock_reranker = MagicMock()
    # Reranker returns index 2 first, then index 0
    mock_reranker.rerank.return_value = [(2, 0.95), (0, 0.7)]

    with patch("paper_ingestion.reranker.get_reranker", return_value=mock_reranker):
        result = await e.rerank_chunks("query", chunks, top_k=2)

    assert len(result) == 2
    assert result[0]["content"] == "C"
    assert result[1]["content"] == "A"


async def test_rerank_chunks_falls_back_on_exception():
    """rerank_chunks returns top_k slice if reranker.rerank raises."""
    e = _make_embedder()
    chunks = [{"content": f"chunk {i}"} for i in range(5)]

    mock_reranker = MagicMock()
    mock_reranker.rerank.side_effect = RuntimeError("model load failed")

    with patch("paper_ingestion.reranker.get_reranker", return_value=mock_reranker):
        result = await e.rerank_chunks("q", chunks, top_k=2)

    assert result == chunks[:2]


# ---------------------------------------------------------------------------
# compute_relevance
# ---------------------------------------------------------------------------


async def test_compute_relevance_returns_max_cosine():
    """compute_relevance returns the max cosine similarity across topic terms."""
    e = _make_embedder()
    # paper=[1,0], term1=[1,0] (score=1.0), term2=[0,1] (score=0.0)
    e.embed_texts = AsyncMock(return_value=[[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])

    score = await e.compute_relevance("paper abstract", ["term-a", "term-b"])
    assert score == 1.0


async def test_compute_relevance_empty_topics_returns_zero():
    """compute_relevance with no topic terms returns 0.0 immediately without calling embed_texts."""
    e = _make_embedder()
    # Replace embed_texts with a mock so we can assert it was never called
    e.embed_texts = AsyncMock()
    score = await e.compute_relevance("paper", [])
    assert score == 0.0
    e.embed_texts.assert_not_awaited()


async def test_compute_relevance_exception_returns_zero():
    """compute_relevance returns 0.0 if embedding raises."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(side_effect=RuntimeError("embed down"))
    score = await e.compute_relevance("paper", ["topic"])
    assert score == 0.0


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------


async def test_ensure_collection_creates_if_not_exists():
    """ensure_collection calls create_collection when the collection is absent."""
    e = _make_embedder()
    e._collection_ensured = False
    # Simulate no existing collections
    e.qdrant.get_collections.return_value = SimpleNamespace(collections=[])

    await e.ensure_collection()

    e.qdrant.create_collection.assert_awaited_once()
    assert e._collection_ensured is True


async def test_ensure_collection_skips_if_already_ensured():
    """ensure_collection is idempotent — skips qdrant calls after first check."""
    e = _make_embedder()
    e._collection_ensured = True

    await e.ensure_collection()

    e.qdrant.get_collections.assert_not_awaited()
    e.qdrant.create_collection.assert_not_awaited()


async def test_ensure_collection_skips_create_if_exists():
    """ensure_collection doesn't create the collection when it already exists."""
    e = _make_embedder()
    e._collection_ensured = False
    existing = SimpleNamespace(name=COLLECTION_NAME)
    e.qdrant.get_collections.return_value = SimpleNamespace(collections=[existing])

    await e.ensure_collection()

    e.qdrant.create_collection.assert_not_awaited()
    assert e._collection_ensured is True
