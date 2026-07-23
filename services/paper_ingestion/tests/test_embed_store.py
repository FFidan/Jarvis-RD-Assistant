"""Boundary-adapter tests for EmbeddingStoreMixin.embed_and_store.

Tests the external boundary: embed_texts (LiteLLM/HTTP) and qdrant.upsert
are mocked; EmbeddingStore itself is real.

CFG-EMBED-1: First-batch failures must be wrapped in EmbeddingBatchError
so pdf_workflow.py:310 handles them uniformly via resume logic.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the project root is importable (matches pattern in test_reembed.py)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from paper_ingestion.ingestion.embed_store import EmbeddingRunContext


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
    completed_batches: list[tuple[int, int]] = []

    async def _record_progress(completed: int, total: int) -> None:
        completed_batches.append((completed, total))

    # Use batch_size=1 so two chunks → two separate batches
    chunks = _make_chunks(n=2)

    with pytest.raises(EmbeddingBatchError) as exc_info:
        await embedder.embed_and_store(
            paper_id=7,
            chunks=chunks,
            user_id=2,
            batch_size=1,
            run_context=EmbeddingRunContext(progress_callback=_record_progress),
        )

    err = exc_info.value
    # First batch completed
    assert len(err.completed_chunks) == 1
    assert len(err.completed_point_ids) == 1
    assert isinstance(err.__cause__, RuntimeError)
    assert "second batch failure" in str(err.__cause__)
    assert completed_batches == [(1, 2)]


# ---------------------------------------------------------------------------
# Resume: skip already-embedded chunks (content + model identity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_and_store_resume_skips_already_persisted_chunks():
    """A retry with the same extraction calls embed_texts ONLY for chunks not
    covered by resume_content — the already-embedded ones are skipped, not
    re-embedded, and still get a deterministic point_id in the result."""
    from paper_ingestion.ingestion.embed_store import EmbeddingBatchError, chunk_point_id
    from paper_ingestion.ingestion.embedding_config import EMBEDDING_DIMENSION

    embedder = _make_embedder()
    chunks = _make_chunks(n=4)  # content: "chunk 0".."chunk 3"

    call_count = 0

    async def _fail_second_batch(texts):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [[0.0] * EMBEDDING_DIMENSION for _ in texts]
        raise RuntimeError("boom")

    embedder.embed_texts = AsyncMock(side_effect=_fail_second_batch)
    embedder.qdrant.upsert = AsyncMock()

    with pytest.raises(EmbeddingBatchError) as exc_info:
        await embedder.embed_and_store(paper_id=9, chunks=chunks, batch_size=2)

    # Batch 0 (chunk_index 0, 1) persisted before batch 1 failed.
    resume_content = {c.chunk_index: c.content for c in exc_info.value.completed_chunks}
    assert set(resume_content) == {0, 1}

    # Retry with the same extraction: only chunks 2 and 3 (unpersisted) are embedded.
    embedder.embed_texts = AsyncMock(return_value=[[0.0] * EMBEDDING_DIMENSION for _ in range(2)])
    embedder.qdrant.upsert = AsyncMock()

    ids = await embedder.embed_and_store(
        paper_id=9,
        chunks=chunks,
        batch_size=2,
        run_context=EmbeddingRunContext(resume_content=resume_content),
    )

    embedder.embed_texts.assert_awaited_once_with(["chunk 2", "chunk 3"])
    assert ids == [chunk_point_id(9, i) for i in range(4)]
    # Skipped chunks must not be re-upserted.
    upserted = embedder.qdrant.upsert.await_args.kwargs["points"]
    assert len(upserted) == 2


@pytest.mark.asyncio
async def test_embed_and_store_reports_each_persisted_or_resumed_batch_once():
    """Batch progress advances only after durable upsert or a valid full skip."""
    from paper_ingestion.ingestion.embedding_config import EMBEDDING_DIMENSION

    embedder = _make_embedder()
    chunks = _make_chunks(n=4)
    embedder.embed_texts = AsyncMock(return_value=[[0.0] * EMBEDDING_DIMENSION for _ in range(2)])
    embedder.qdrant.upsert = AsyncMock()
    completed_batches: list[tuple[int, int]] = []

    async def _record_progress(completed: int, total: int) -> None:
        completed_batches.append((completed, total))

    await embedder.embed_and_store(
        paper_id=10,
        chunks=chunks,
        batch_size=2,
        run_context=EmbeddingRunContext(
            resume_content={0: "chunk 0", 1: "chunk 1"},
            progress_callback=_record_progress,
        ),
    )

    assert completed_batches == [(1, 2), (2, 2)]
    embedder.embed_texts.assert_awaited_once_with(["chunk 2", "chunk 3"])
    embedder.qdrant.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_callback_failure_preserves_persisted_batch_for_resume():
    """Observer failure after upsert still exposes the durable batch to callers."""
    from paper_ingestion.ingestion.embed_store import EmbeddingBatchError, chunk_point_id
    from paper_ingestion.ingestion.embedding_config import EMBEDDING_DIMENSION

    embedder = _make_embedder()
    chunks = _make_chunks(n=2)
    embedder.embed_texts = AsyncMock(return_value=[[0.0] * EMBEDDING_DIMENSION for _ in chunks])
    embedder.qdrant.upsert = AsyncMock()

    async def _fail_progress(_completed: int, _total: int) -> None:
        raise RuntimeError("progress backend unavailable")

    with pytest.raises(EmbeddingBatchError) as exc_info:
        await embedder.embed_and_store(
            paper_id=11,
            chunks=chunks,
            batch_size=2,
            run_context=EmbeddingRunContext(progress_callback=_fail_progress),
        )

    err = exc_info.value
    expected_ids = [chunk_point_id(11, chunk.chunk_index) for chunk in chunks]
    assert err.completed_chunks == chunks
    assert err.completed_point_ids == expected_ids
    assert isinstance(err.__cause__, RuntimeError)
    assert "progress backend unavailable" in str(err.__cause__)
    embedder.qdrant.upsert.assert_awaited_once()

    resume_content = {chunk.chunk_index: chunk.content for chunk in err.completed_chunks}
    embedder.embed_texts = AsyncMock()
    embedder.qdrant.upsert = AsyncMock()

    point_ids = await embedder.embed_and_store(
        paper_id=11,
        chunks=chunks,
        batch_size=2,
        run_context=EmbeddingRunContext(resume_content=resume_content),
    )

    assert point_ids == expected_ids
    embedder.embed_texts.assert_not_awaited()
    embedder.qdrant.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_and_store_resume_reembeds_changed_content():
    """A chunk whose content changed since the prior run is re-embedded even
    though resume_content has an entry for its chunk_index; an unchanged
    chunk is skipped and still returns its deterministic point_id."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedding_config import EMBEDDING_DIMENSION

    embedder = _make_embedder()
    chunks = _make_chunks(n=2)  # content: "chunk 0", "chunk 1"
    resume_content = {0: "stale content", 1: "chunk 1"}  # chunk 1 unchanged

    embedder.embed_texts = AsyncMock(return_value=[[0.0] * EMBEDDING_DIMENSION])
    embedder.qdrant.upsert = AsyncMock()

    ids = await embedder.embed_and_store(
        paper_id=3,
        chunks=chunks,
        run_context=EmbeddingRunContext(resume_content=resume_content),
    )

    embedder.embed_texts.assert_awaited_once_with(["chunk 0"])
    assert ids == [chunk_point_id(3, 0), chunk_point_id(3, 1)]
    upserted = embedder.qdrant.upsert.await_args.kwargs["points"]
    assert len(upserted) == 1


@pytest.mark.asyncio
async def test_embed_and_store_persists_content_identity_in_vector_payload():
    """Stored vectors carry content identity and complete visibility metadata."""
    from paper_ingestion.ingestion.embedding_config import (
        EMBEDDING_DIMENSION,
        EMBEDDING_MODEL_NAME,
    )
    from paper_ingestion.ingestion.payload_schema import VectorVisibility

    embedder = _make_embedder()
    embedder.embed_texts = AsyncMock(return_value=[[0.0] * EMBEDDING_DIMENSION])
    embedder.qdrant.upsert = AsyncMock()

    await embedder.embed_and_store(
        paper_id=8,
        chunks=_make_chunks(n=1),
        user_id=3,
        visibility=VectorVisibility(
            source_type="arxiv",
            visibility_scope="public",
            visibility_generation="3" * 32,
        ),
    )

    point = embedder.qdrant.upsert.await_args.kwargs["points"][0]
    expected = hashlib.sha256(f"{EMBEDDING_MODEL_NAME}\0chunk 0".encode()).hexdigest()
    assert point.payload["embedding_fingerprint"] == expected
    assert point.payload["source_type"] == "arxiv"
    assert point.payload["visibility_scope"] == "public"
    assert point.payload["visibility_generation"] == "3" * 32
    assert embedder.qdrant.upsert.await_args.kwargs["wait"] is True
