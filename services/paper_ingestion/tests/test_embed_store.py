"""Boundary-adapter tests for EmbeddingStoreMixin.embed_and_store.

Tests the external boundary: embed_texts (LiteLLM/HTTP) and qdrant.upsert
are mocked; EmbeddingStore itself is real.

CFG-EMBED-1: First-batch failures must be wrapped in EmbeddingBatchError
so pdf_workflow.py:310 handles them uniformly via resume logic.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the project root is importable (matches pattern in test_reembed.py)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Qdrant model stubs — scoped via monkeypatch so they don't bleed across files
# ---------------------------------------------------------------------------


class _FakePointStruct:
    def __init__(self, *, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload


class _FakeVectorParams:
    def __init__(self, *, size, distance):
        self.size = size
        self.distance = distance


_fake_qdrant_models = SimpleNamespace(
    Distance=SimpleNamespace(COSINE="cosine"),
    FieldCondition=MagicMock,
    Filter=MagicMock,
    MatchValue=MagicMock,
    PointStruct=_FakePointStruct,
    VectorParams=_FakeVectorParams,
)

_fake_qdrant_client_mod = SimpleNamespace(AsyncQdrantClient=MagicMock())


@pytest.fixture(autouse=True)
def _install_qdrant_stubs(monkeypatch):
    """Scope qdrant_client stubs to each test; evict embed_store so imports
    always see the real module backed by these stubs."""
    monkeypatch.setitem(sys.modules, "qdrant_client", _fake_qdrant_client_mod)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", _fake_qdrant_models)
    # Evict the mixin module so it re-imports with the stubbed qdrant_client
    monkeypatch.delitem(sys.modules, "paper_ingestion.ingestion.embed_store", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunks(n: int = 2):
    """Return n minimal ChunkForEmbedding instances."""
    from paper_ingestion.models import ChunkForEmbedding

    return [
        ChunkForEmbedding(
            chunk_index=i,
            content=f"chunk {i}",
            page_number=1,
            start_char=i * 10,
            end_char=i * 10 + 7,
        )
        for i in range(n)
    ]


def _make_embedder():
    """Construct a minimal Embedder with a mock qdrant client and http_client.

    We cannot call Embedder.__init__ directly because tiktoken.get_encoding
    may not be available in CI. Instead we compose the mixin onto a lightweight
    stand-in that provides the shared state Embedder.__init__ normally sets.
    """
    from paper_ingestion.ingestion.embed_store import EmbeddingStoreMixin

    class _MinimalEmbedder(EmbeddingStoreMixin):
        def __init__(self):
            self.qdrant = AsyncMock()
            self.http_client = MagicMock()
            self._collection_ensured = True  # skip ensure_collection in tests
            self._collection_lock = asyncio.Lock()

    return _MinimalEmbedder()


# ---------------------------------------------------------------------------
# CFG-EMBED-1: First-batch failure wrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_batch_failure_raises_embedding_batch_error():
    """When embed_texts raises on the very first batch, embed_and_store must
    raise EmbeddingBatchError (not the raw exception) so pdf_workflow.py:310
    can apply uniform resume logic regardless of which batch failed.

    Test shape: boundary-adapter — external boundary (embed_texts / Qdrant
    upsert) is mocked; EmbeddingStoreMixin is the real implementation.
    """
    from paper_ingestion.ingestion.embed_store import EmbeddingBatchError

    embedder = _make_embedder()
    # Simulate LiteLLM / Qdrant being down on the first (and only) batch
    embedder.embed_texts = AsyncMock(side_effect=RuntimeError("qdrant down"))

    chunks = _make_chunks(n=2)

    with pytest.raises(EmbeddingBatchError) as exc_info:
        await embedder.embed_and_store(paper_id=1, chunks=chunks, user_id=1)

    err = exc_info.value
    assert err.completed_chunks == [], "completed_chunks must be empty on first-batch failure"
    assert err.completed_point_ids == [], "completed_point_ids must be empty on first-batch failure"
    # Original exception must be chained
    assert isinstance(err.__cause__, RuntimeError)
    assert "qdrant down" in str(err.__cause__)


@pytest.mark.asyncio
async def test_first_batch_failure_message_contains_context():
    """EmbeddingBatchError message for first-batch failure should note 0 chunks persisted."""
    from paper_ingestion.ingestion.embed_store import EmbeddingBatchError

    embedder = _make_embedder()
    embedder.embed_texts = AsyncMock(side_effect=RuntimeError("timeout"))

    chunks = _make_chunks(n=1)

    with pytest.raises(EmbeddingBatchError) as exc_info:
        await embedder.embed_and_store(paper_id=42, chunks=chunks, user_id=5)

    assert "0 chunks persisted" in str(exc_info.value)


# ---------------------------------------------------------------------------
# None-dimension guard: unknown collection dimension must skip the mismatch check
# ---------------------------------------------------------------------------


def test_raise_for_collection_dimension_mismatch_skips_when_dimension_unknown():
    """A None current_dimension means "unknown" (Qdrant info lacked a size) and
    must NOT raise — only a concrete, mismatching dimension is an error."""
    from paper_ingestion.ingestion.embedding_config import (
        raise_for_collection_dimension_mismatch,
    )

    # Must not raise: None == "we couldn't determine the dimension", skip the check.
    raise_for_collection_dimension_mismatch("c", None, expected_dimension=2560)


def test_raise_for_collection_dimension_mismatch_still_raises_on_real_mismatch():
    """Regression guard: a concrete mismatching dimension still raises."""
    from paper_ingestion.ingestion.embedding_config import (
        raise_for_collection_dimension_mismatch,
    )

    with pytest.raises(RuntimeError):
        raise_for_collection_dimension_mismatch("c", 1024, expected_dimension=2560)


@pytest.mark.asyncio
async def test_subsequent_batch_failure_still_raises_embedding_batch_error():
    """Mid-batch failure (after first batch succeeds) continues to raise
    EmbeddingBatchError with non-empty completed lists — regression guard."""
    from paper_ingestion.ingestion.embed_store import EmbeddingBatchError
    from paper_ingestion.ingestion.embedding_config import EMBEDDING_DIMENSION

    embedder = _make_embedder()

    call_count = 0

    async def _embed_texts_side_effect(texts):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First batch succeeds: return dummy embeddings of correct dimension
            return [[0.0] * EMBEDDING_DIMENSION for _ in texts]
        # Second batch fails
        raise RuntimeError("second batch failure")

    embedder.embed_texts = AsyncMock(side_effect=_embed_texts_side_effect)
    embedder.qdrant.upsert = AsyncMock()  # upsert succeeds for first batch

    # Use batch_size=1 so two chunks → two separate batches
    chunks = _make_chunks(n=2)

    with pytest.raises(EmbeddingBatchError) as exc_info:
        await embedder.embed_and_store(paper_id=7, chunks=chunks, user_id=2, batch_size=1)

    err = exc_info.value
    # First batch completed
    assert len(err.completed_chunks) == 1
    assert len(err.completed_point_ids) == 1
    assert isinstance(err.__cause__, RuntimeError)
    assert "second batch failure" in str(err.__cause__)
