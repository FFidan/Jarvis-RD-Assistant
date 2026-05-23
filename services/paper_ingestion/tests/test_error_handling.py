"""Tests for error handling in embedder and search chunks."""

from unittest.mock import AsyncMock

import httpx
import pytest
from paper_ingestion.ingestion.embedder import Embedder


# ---------------------------------------------------------------------------
# embed_texts error handling
# ---------------------------------------------------------------------------


async def test_embed_texts_timeout():
    """embed_texts wraps httpx.TimeoutException as RuntimeError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    mock_http.post.side_effect = httpx.TimeoutException("read timed out")

    with pytest.raises(RuntimeError, match="Embedding service timed out"):
        await embedder.embed_texts(["test text"])


async def test_embed_texts_connection_error():
    """embed_texts wraps httpx.ConnectError as RuntimeError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    mock_http.post.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(RuntimeError, match="Embedding service unavailable"):
        await embedder.embed_texts(["test text"])


# ---------------------------------------------------------------------------
# search_chunks error handling
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
