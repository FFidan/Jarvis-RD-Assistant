"""Tests for the Embedder text chunking logic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
from paper_ingestion.embedder import Embedder

# D3-05: shared fake — replaces the local _FakeEncoding that was duplicated 3×.
from tests._embedder_fakes import _FakeEncoding


# D3-07 deleted: test_chunk_text_basic
# Superseded by test_embedder_behavior.py::test_chunk_text_short_text_is_single_chunk (line 235)
# and test_embedder_char_c1.py::test_chunk_text_exact_boundary_snapshot (line 74), which cover
# chunk_index, start_char=0, content membership, and end_char==len(text) with exact pinned values.


async def test_chunk_text_with_page_boundaries():
    """Embedder.chunk_text assigns page numbers based on boundaries."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder._encoding = _FakeEncoding()  # type: ignore[assignment]

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
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * EMBEDDING_DIMENSION}]}
    mock_http = AsyncMock()
    mock_http.post.return_value = response
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    result = await embedder.embed_texts(["test text"])

    assert result == [[0.1] * EMBEDDING_DIMENSION]
    mock_http.post.assert_awaited_once()
    call = mock_http.post.call_args
    assert call.args[0] == "http://litellm.test:4000/v1/embeddings"
    assert call.kwargs["json"] == {"model": "embed", "input": ["test text"]}
    assert call.kwargs["headers"] == {}
    assert call.kwargs["timeout"].read >= 300.0


# D3-07 deleted: test_search_similar_skips_hits_without_dict_payload
# Superseded by test_embedder_behavior.py::test_search_similar_skips_null_payload (line 446)
# and test_embedder_behavior.py::test_search_similar_content_truncated_to_200 (line 459).

# D3-07 deleted: test_search_chunks_global_returns_empty_on_qdrant_failure
# Superseded by test_embedder_behavior.py::test_search_chunks_global_empty_on_qdrant_error (line 529).


async def test_search_chunks_global_filters_by_user_id():
    """user_id kwarg produces a Filter(should=[user_id==N OR is_null])."""
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points.return_value = SimpleNamespace(points=[])
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])

    await embedder.search_chunks_global("query", limit=10, user_id=42)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf is not None
    assert len(qf.should) == 2
    assert any(
        isinstance(c, FieldCondition)
        and c.key == "user_id"
        and isinstance(c.match, MatchValue)
        and c.match.value == 42
        for c in qf.should
    )
    assert any(isinstance(c, IsNullCondition) for c in qf.should)


async def test_search_similar_filters_user_and_excludes_paper():
    """search_similar composes must_not[paper_id] AND should[user_id OR null]."""
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points.return_value = SimpleNamespace(points=[])
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])

    await embedder.search_similar("q", limit=5, paper_id_filter=11, user_id=7)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf is not None
    assert qf.must_not is not None and len(qf.must_not) == 1
    paper_clause = qf.must_not[0]
    assert isinstance(paper_clause, FieldCondition)
    assert paper_clause.key == "paper_id"
    assert isinstance(paper_clause.match, MatchValue)
    assert paper_clause.match.value == 11
    assert qf.should is not None and len(qf.should) == 2
    assert any(
        isinstance(c, FieldCondition)
        and c.key == "user_id"
        and isinstance(c.match, MatchValue)
        and c.match.value == 7
        for c in qf.should
    )
    assert any(isinstance(c, IsNullCondition) for c in qf.should)


async def test_hybrid_search_threads_user_id_to_semantic_leg():
    """hybrid_search forwards user_id to search_chunks_global only."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db_pool.acquire.return_value.__aenter__.return_value = conn

    await embedder.hybrid_search("q", db_pool=db_pool, limit=5, user_id=42)

    assert embedder.search_chunks_global.call_args.kwargs["user_id"] == 42


# D3-07 deleted: test_compute_relevance_uses_max_cosine_similarity
# Superseded by test_embedder_behavior.py::test_compute_relevance_returns_max_cosine (line 744).

# D3-07 deleted: test_discover_from_seeds_deduplicates_and_ignores_null_payloads
# Superseded by test_embedder_behavior.py::test_discover_from_seeds_deduplicates_by_paper_id (line 621)
# and test_embedder_char_c1.py::test_discover_from_seeds_exact_dedup_and_order (line 372).

# D3-07 deleted: test_chunk_text_offsets_align_with_decoded_window (PI-CORE-005)
# Superseded by test_embedder_char_c1.py::test_chunk_text_exact_boundary_snapshot (line 74), which
# pins exact force-split offsets, content, and chunk_index sequence; and
# test_embedder_behavior.py::test_chunk_text_page_boundaries_assigned (line 257) for page_number.


# D3-10 deleted: test_hybrid_search_pagination_returns_results_at_offset_20
# Superseded by test_hybrid_pagination.py::test_pagination_page* suite, which
# covers offset semantics with dedicated non-overlap, page-boundary, and
# candidate-limit assertions (canonical per audit D3-10).
