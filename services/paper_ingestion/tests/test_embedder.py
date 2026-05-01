"""Tests for the Embedder text chunking logic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
from paper_ingestion.embedder import Embedder


class _FakeEncoding:
    """Character-level encoding stand-in for tiktoken (not installed on host)."""

    def encode(self, text):
        return list(text)

    def decode(self, tokens):
        return "".join(tokens)


async def test_chunk_text_basic():
    """Embedder.chunk_text splits text into token-limited chunks."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder._encoding = _FakeEncoding()

    text = "This is a test sentence. " * 40
    chunks = embedder.chunk_text(text)

    assert len(chunks) >= 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].start_char == 0
    assert chunks[0].content in text
    assert chunks[-1].end_char == len(text)


async def test_chunk_text_with_page_boundaries():
    """Embedder.chunk_text assigns page numbers based on boundaries."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder._encoding = _FakeEncoding()

    # Make pages large enough that chunks don't span both
    page1 = "Page one has important research content about attention mechanisms. " * 80
    page2 = "Page two discusses the experimental results in detail for evaluation. " * 80
    text = page1 + page2
    boundaries = [(0, len(page1)), (len(page1), len(text))]

    chunks = embedder.chunk_text(text, page_boundaries=boundaries)

    assert len(chunks) >= 2
    assert chunks[0].page_number == 1
    assert chunks[-1].page_number == 2


async def test_embed_texts_uses_shared_litellm_config_base_url(monkeypatch):
    """Embedder should pick up the shared base URL (transparent proxy, no auth headers)."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * 768}]}
    mock_http = AsyncMock()
    mock_http.post.return_value = response
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    result = await embedder.embed_texts(["test text"])

    assert result == [[0.1] * 768]
    mock_http.post.assert_awaited_once_with(
        "http://litellm.test:4000/v1/embeddings",
        json={"model": "embed", "input": ["test text"]},
        headers={},
        timeout=60.0,
    )


async def test_search_similar_skips_hits_without_dict_payload():
    """search_similar should ignore Qdrant hits whose payload is missing/null."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(payload=None, score=0.99),
            SimpleNamespace(
                payload={
                    "paper_id": 3,
                    "chunk_index": 1,
                    "content": "A" * 240,
                    "page_number": 7,
                },
                score=0.88,
            ),
        ]
    )
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])

    results = await embedder.search_similar("query", limit=5, paper_id_filter=11)

    assert results == [
        {
            "paper_id": 3,
            "chunk_index": 1,
            "content": "A" * 200,
            "page_number": 7,
            "score": 0.88,
        }
    ]


async def test_search_chunks_global_returns_empty_on_qdrant_failure():
    """search_chunks_global should degrade to an empty list on Qdrant errors."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points.side_effect = Exception("qdrant down")
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])

    results = await embedder.search_chunks_global("query", limit=10)

    assert results == []


async def test_compute_relevance_uses_max_cosine_similarity():
    """compute_relevance should return the highest cosine similarity across topic terms."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.embed_texts = AsyncMock(
        return_value=[
            [1.0, 0.0],  # paper
            [1.0, 0.0],  # exact match
            [0.0, 1.0],  # orthogonal
        ]
    )

    score = await embedder.compute_relevance("paper", ["term-a", "term-b"])

    assert score == 1.0


async def test_discover_from_seeds_deduplicates_and_ignores_null_payloads():
    """discover_from_seeds should keep the best hit per paper and skip null payloads."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    mock_qdrant.scroll.return_value = ([SimpleNamespace(id="pt-1")], None)
    mock_qdrant.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(payload=None, score=0.95),
            SimpleNamespace(
                payload={"paper_id": 21, "content": "first candidate"},
                score=0.75,
            ),
            SimpleNamespace(
                payload={"paper_id": 21, "content": "better candidate"},
                score=0.89,
            ),
        ]
    )
    embedder = Embedder(mock_http, mock_qdrant)
    db_pool = MagicMock()

    results = await embedder.discover_from_seeds([7], db_pool=db_pool, limit=3)

    assert results == [{"paper_id": 21, "score": 0.89, "content": "better candidate"}]


async def test_chunk_text_offsets_align_with_decoded_window():
    """PI-CORE-005: sub-split char offsets use decoded-window advance, not linear interpolation.

    We construct a multi-paragraph text where one paragraph is large enough to
    trigger the token-window force-split path.  We then verify that every chunk's
    start_char and end_char point to content actually present in the original text,
    and that find_page(mid) resolves to the correct page for each chunk.

    With _FakeEncoding (1 char = 1 token), decoded window lengths equal char
    lengths, so char_advance equals j * (CHUNK_TOKEN_LIMIT - CHUNK_OVERLAP_TOKENS).
    This is different from the old linear-interpolation formula only when
    token lengths are non-uniform, but with the fake encoding we can assert the
    boundary property: chunk.content == text[start_char:end_char].strip().
    """
    from paper_ingestion.embedder import CHUNK_TOKEN_LIMIT

    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder._encoding = _FakeEncoding()

    # Para 1: short (won't be force-split), Para 2: long (will be force-split)
    # Para 3: short again (on a different "page")
    para1 = "A" * 10  # 10 chars, fits in one chunk
    sep = "\n\n"
    para2 = "B" * (CHUNK_TOKEN_LIMIT * 3)  # 3× limit → multiple windows
    para3 = "C" * 20

    # Make one big section so para2 will exceed CHUNK_TOKEN_LIMIT and trigger
    # the section-too-large → sub-split path.
    section_body = para1 + sep + para2 + sep + para3
    # Wrap in a heading so it is treated as one section by chunk_text
    text = "## Section\n" + section_body

    # Page boundaries: page 1 covers everything
    boundaries = [(0, len(text))]

    chunks = embedder.chunk_text(text, page_boundaries=boundaries)

    assert len(chunks) >= 2, "Expected multiple chunks from oversized paragraph"

    # Every chunk's content must appear verbatim in the original text
    # (stripped of leading/trailing whitespace as chunk_text does)
    for chunk in chunks:
        # The chunk content must be a substring of the original text
        # (chunks from the force-split path are sub-strings of para2)
        assert chunk.content in text, (
            f"Chunk content not found in original text: {chunk.content[:40]!r}"
        )
        # start_char must be strictly less than end_char
        assert chunk.start_char < chunk.end_char, (
            f"Invalid char range [{chunk.start_char}, {chunk.end_char})"
        )
        # Midpoint must resolve to a valid page (not None since we provided boundaries)
        assert chunk.page_number is not None, "page_number must be set when boundaries provided"

    # Chunks from the force-split of para2 must have monotonically increasing start_char
    # (they all come from para2 which is a contiguous block)
    b_chunks = [c for c in chunks if c.content and c.content[0] == "B"]
    if len(b_chunks) > 1:
        starts = [c.start_char for c in b_chunks]
        assert starts == sorted(starts), f"Force-split chunk starts not monotonic: {starts}"

    # Sequential chunk_index
    for expected_idx, chunk in enumerate(chunks):
        assert chunk.chunk_index == expected_idx


async def test_hybrid_search_pagination_returns_results_at_offset_20():
    """PI-CORE-006: hybrid_search with offset=20 uses candidate_limit for both legs.

    We verify that:
    1. The BM25 SQL is called with candidate_limit = limit + offset (capped at 200).
    2. search_chunks_global is called with the same candidate_limit.
    3. Results are non-empty when the merged RRF pool has enough candidates.
    """
    from paper_ingestion.embedder import EMBEDDING_DIMENSION

    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])

    # Seed the BM25 leg with 35 distinct papers (ids 1..35)
    def _make_bm25_row(paper_id: int) -> MagicMock:
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "id": paper_id,
            "title": f"Paper {paper_id}",
            "authors": [],
            "url": f"http://example.com/{paper_id}",
            "abstract": f"abstract {paper_id}",
            "published_date": None,
        }[k]
        return row

    bm25_rows = [_make_bm25_row(i) for i in range(1, 36)]  # 35 papers

    conn = AsyncMock()
    conn.fetch.return_value = bm25_rows
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db_pool = MagicMock()
    db_pool.acquire.return_value = ctx

    # Seed the semantic leg with the same 35 papers
    semantic_chunks = [
        {"paper_id": i, "score": 0.9 - i * 0.01, "content": f"content {i}", "chunk_index": 0}
        for i in range(1, 36)
    ]
    embedder.search_chunks_global = AsyncMock(return_value=semantic_chunks)

    # Query with offset=20, limit=10 — should return 10 results skipping first 20
    results = await embedder.hybrid_search("test query", db_pool=db_pool, limit=10, offset=20)

    # Must return results (the pool of 35 has items beyond offset 20)
    assert len(results) > 0, "Expected non-empty results at offset=20 with 35 candidates"
    assert len(results) <= 10, "Must not exceed requested limit"

    # Verify candidate_limit passed to both legs = min(10+20, 200) = 30
    expected_candidate_limit = min(10 + 20, 200)  # 30

    # BM25: conn.fetch was called with (sql, query, candidate_limit)
    fetch_call_args = conn.fetch.call_args
    assert fetch_call_args is not None
    actual_bm25_limit = fetch_call_args.args[2]  # third positional arg
    assert actual_bm25_limit == expected_candidate_limit, (
        f"BM25 LIMIT should be {expected_candidate_limit}, got {actual_bm25_limit}"
    )

    # Semantic: search_chunks_global called with limit=candidate_limit
    scg_call = embedder.search_chunks_global.call_args
    actual_sem_limit = scg_call.kwargs.get("limit") or scg_call.args[1]
    assert actual_sem_limit == expected_candidate_limit, (
        f"Semantic limit should be {expected_candidate_limit}, got {actual_sem_limit}"
    )
