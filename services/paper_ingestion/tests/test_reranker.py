"""Tests for the cross-encoder reranker module."""

from unittest.mock import AsyncMock, MagicMock, patch

from paper_ingestion.embedder import Embedder
from paper_ingestion.reranker import Reranker, get_reranker


def test_rerank_empty_passages():
    """Reranking empty list returns empty list."""
    reranker = Reranker.__new__(Reranker)
    result = reranker.rerank("query", [], top_k=5)
    assert result == []


def test_rerank_returns_top_k():
    """Reranking returns at most top_k results."""
    reranker = Reranker.__new__(Reranker)
    mock_model = MagicMock()
    mock_model.predict.return_value = [0.9, 0.1, 0.5, 0.8, 0.3]
    reranker._model = mock_model

    result = reranker.rerank("query", ["a", "b", "c", "d", "e"], top_k=3)
    assert len(result) == 3
    # Should be sorted by score descending
    assert result[0] == (0, 0.9)  # "a" had highest score
    assert result[1] == (3, 0.8)  # "d" was second
    assert result[2] == (2, 0.5)  # "c" was third


def test_rerank_preserves_original_indices():
    """Reranking preserves original indices for mapping back."""
    reranker = Reranker.__new__(Reranker)
    mock_model = MagicMock()
    mock_model.predict.return_value = [0.2, 0.9, 0.5]
    reranker._model = mock_model

    result = reranker.rerank("query", ["low", "high", "mid"], top_k=3)
    assert result[0][0] == 1  # "high" was at index 1
    assert result[1][0] == 2  # "mid" was at index 2
    assert result[2][0] == 0  # "low" was at index 0


def test_rerank_builds_correct_pairs():
    """Reranker passes correct query-passage pairs to the model."""
    reranker = Reranker.__new__(Reranker)
    mock_model = MagicMock()
    mock_model.predict.return_value = [0.5, 0.8]
    reranker._model = mock_model

    reranker.rerank("my query", ["passage A", "passage B"], top_k=2)
    mock_model.predict.assert_called_once_with(
        [["my query", "passage A"], ["my query", "passage B"]]
    )


async def test_rerank_chunks_fallback_when_unavailable():
    """rerank_chunks falls back to truncation when reranker unavailable."""
    embedder = Embedder(AsyncMock(), AsyncMock())
    chunks = [{"content": f"chunk {i}"} for i in range(10)]

    with patch("paper_ingestion.ingestion.reranker.get_reranker", return_value=None):
        result = await embedder.rerank_chunks("query", chunks, top_k=5)
    assert len(result) == 5
    assert result == chunks[:5]


async def test_rerank_chunks_applies_reranking():
    """rerank_chunks reorders chunks when reranker is available."""
    embedder = Embedder(AsyncMock(), AsyncMock())
    chunks = [
        {"content": "low relevance"},
        {"content": "high relevance"},
        {"content": "medium relevance"},
    ]

    mock_reranker = MagicMock()
    mock_reranker.rerank.return_value = [(1, 0.9), (2, 0.5)]

    with patch("paper_ingestion.ingestion.reranker.get_reranker", return_value=mock_reranker):
        result = await embedder.rerank_chunks("query", chunks, top_k=2)
    assert len(result) == 2
    assert result[0] == chunks[1]  # "high relevance" first
    assert result[1] == chunks[2]  # "medium relevance" second


async def test_rerank_chunks_skips_when_fewer_than_top_k():
    """rerank_chunks returns input as-is when len(chunks) <= top_k."""
    embedder = Embedder(AsyncMock(), AsyncMock())
    chunks = [{"content": "only one"}]

    mock_reranker = MagicMock()
    with patch("paper_ingestion.ingestion.reranker.get_reranker", return_value=mock_reranker):
        result = await embedder.rerank_chunks("query", chunks, top_k=5)
    assert result == chunks
    mock_reranker.rerank.assert_not_called()


async def test_rerank_chunks_fallback_on_exception():
    """rerank_chunks falls back to truncation on reranker exception."""
    embedder = Embedder(AsyncMock(), AsyncMock())
    chunks = [{"content": f"chunk {i}"} for i in range(10)]

    mock_reranker = MagicMock()
    mock_reranker.rerank.side_effect = RuntimeError("model crashed")

    with patch("paper_ingestion.ingestion.reranker.get_reranker", return_value=mock_reranker):
        result = await embedder.rerank_chunks("query", chunks, top_k=5)
    assert len(result) == 5
    assert result == chunks[:5]


def test_get_reranker_returns_none_without_sentence_transformers():
    """get_reranker returns None when model loading fails."""
    import paper_ingestion.reranker as reranker_mod

    # Reset singleton state so the load attempt runs fresh.
    original_state = reranker_mod._reranker_state
    reranker_mod._reranker_state = reranker_mod._RerankerState()
    try:
        with (
            patch.dict("os.environ", {"RERANKER_ENABLED": "true"}),
            patch.object(Reranker, "_load_model_if_needed", side_effect=ImportError("no module")),
        ):
            result = get_reranker()
    finally:
        reranker_mod._reranker_state = original_state

    assert result is None
