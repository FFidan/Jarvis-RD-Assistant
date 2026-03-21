"""Tests for the Embedder text chunking logic."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

class _LocalFakeEncoding:
    def encode(self, text):
        return list(text)

    def decode(self, tokens):
        return "".join(tokens)

if "tiktoken" not in sys.modules:
    fake_tiktoken = types.ModuleType("tiktoken")

    class _FakeEncoding:
        def encode(self, text):
            return list(text)

        def decode(self, tokens):
            return "".join(tokens)

    fake_tiktoken.get_encoding = lambda _name: _FakeEncoding()
    sys.modules["tiktoken"] = fake_tiktoken

if "qdrant_client" not in sys.modules:
    fake_qdrant = types.ModuleType("qdrant_client")
    fake_qdrant.AsyncQdrantClient = object
    sys.modules["qdrant_client"] = fake_qdrant

if "qdrant_client.models" not in sys.modules:
    fake_qdrant_models = types.ModuleType("qdrant_client.models")
    fake_qdrant_models.Distance = types.SimpleNamespace(COSINE="cosine")
    fake_qdrant_models.FieldCondition = MagicMock()
    fake_qdrant_models.Filter = MagicMock()
    fake_qdrant_models.MatchAny = MagicMock()
    fake_qdrant_models.MatchValue = MagicMock()
    fake_qdrant_models.PointIdsList = object
    fake_qdrant_models.PointStruct = object
    fake_qdrant_models.RecommendInput = MagicMock()
    fake_qdrant_models.RecommendQuery = MagicMock()
    fake_qdrant_models.RecommendStrategy = types.SimpleNamespace(AVERAGE_VECTOR="average")
    fake_qdrant_models.VectorParams = object
    sys.modules["qdrant_client.models"] = fake_qdrant_models

from app.embedder import Embedder


async def test_chunk_text_basic():
    """Embedder.chunk_text splits text into token-limited chunks."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder._encoding = _LocalFakeEncoding()

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
    embedder._encoding = _LocalFakeEncoding()

    # Make pages large enough that chunks don't span both
    page1 = "Page one has important research content about attention mechanisms. " * 80
    page2 = "Page two discusses the experimental results in detail for evaluation. " * 80
    text = page1 + page2
    boundaries = [(0, len(page1)), (len(page1), len(text))]

    chunks = embedder.chunk_text(text, page_boundaries=boundaries)

    assert len(chunks) >= 2
    assert chunks[0].page_number == 1
    assert chunks[-1].page_number == 2


async def test_embed_texts_uses_shared_litellm_config_fallback(monkeypatch):
    """Embedder should pick up the shared base URL and MASTER_KEY fallback."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-secret")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [{"index": 0, "embedding": [0.1] * 768}]
    }
    mock_http = AsyncMock()
    mock_http.post.return_value = response
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    result = await embedder.embed_texts(["test text"])

    assert result == [[0.1] * 768]
    mock_http.post.assert_awaited_once_with(
        "http://litellm.test:4000/v1/embeddings",
        json={"model": "embed", "input": ["test text"]},
        headers={"Authorization": "Bearer master-secret"},
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

    assert results == [
        {"paper_id": 21, "score": 0.89, "content": "better candidate"}
    ]
