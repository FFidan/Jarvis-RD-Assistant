"""Unit tests for shared PDF workflow helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import torch

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.services.pdf_workflow import advisory_lock, run_process_pdf


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Create a pool mock whose acquire() yields the provided connection."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


@pytest.mark.asyncio
async def test_advisory_lock_unlocks_even_after_error():
    """advisory_lock always releases the PostgreSQL advisory lock."""
    conn = AsyncMock()

    with pytest.raises(RuntimeError, match="boom"):
        async with advisory_lock(conn, 3, 7):
            raise RuntimeError("boom")

    assert conn.execute.await_args_list[0].args == ("SELECT pg_advisory_lock($1, $2)", 3, 7)
    assert conn.execute.await_args_list[1].args == ("SELECT pg_advisory_unlock($1, $2)", 3, 7)


@pytest.mark.asyncio
async def test_run_process_pdf_returns_already_processed_without_force():
    """Existing chunks short-circuit without calling the processor."""
    conn = AsyncMock()
    conn.fetchval.return_value = 4
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    embedder = MagicMock()

    result = await run_process_pdf(
        paper_id=5,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=False,
    )

    assert result == {"paper_id": 5, "chunk_count": 4, "status": "already_processed"}
    pdf_processor.process.assert_not_called()


@pytest.mark.asyncio
async def test_run_process_pdf_keeps_new_chunks_when_qdrant_cleanup_fails():
    """Force-reprocessing replaces DB chunks even if stale vector cleanup fails."""
    conn = AsyncMock()
    conn.fetchval.return_value = 2
    conn.fetch.return_value = [{"embedding_id": "vec-1"}]
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool = _make_pool(conn)
    chunks = [
        SimpleNamespace(
            chunk_index=0,
            content="New chunk",
            page_number=1,
            start_char=0,
            end_char=9,
        )
    ]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", chunks, ["vec-new"]))
    embedder = MagicMock()
    embedder.qdrant.delete = AsyncMock(side_effect=RuntimeError("qdrant down"))

    result = await run_process_pdf(
        paper_id=5,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=True,
    )

    assert result == {"paper_id": 5, "chunk_count": 1, "status": "processed"}
    conn.execute.assert_any_await("DELETE FROM paper_chunks WHERE paper_id = $1", 5)
    conn.executemany.assert_awaited_once()
    embedder.qdrant.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_process_pdf_wraps_embedding_failures():
    """Embedding errors keep sanitized cause detail for operators."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(
        side_effect=RuntimeError("Embedding service error (HTTP 401): bad auth")
    )
    embedder = MagicMock()

    with pytest.raises(RuntimeError) as exc_info:
        await run_process_pdf(
            paper_id=9,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )

    message = str(exc_info.value)
    assert "Embedding service error (HTTP 401): bad auth" in message
    assert "LITELLM_MASTER_KEY" in message


@pytest.mark.asyncio
async def test_run_process_pdf_persists_completed_chunks_on_embedding_batch_error():
    """B-EMBED per-batch resume: when an EmbeddingBatchError carries chunks
    from batches that DID upsert, run_process_pdf must persist those chunk
    rows (so a retry resumes via Phase-1 idempotency + ON CONFLICT) before
    re-raising a recoverable RuntimeError."""
    from paper_ingestion.ingestion.embedder import EmbeddingBatchError

    conn = AsyncMock()
    conn.fetchval.return_value = 0  # no existing chunks
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool = _make_pool(conn)

    completed_chunks = [
        ChunkForEmbedding(
            chunk_index=0,
            content="Embedded chunk A",
            page_number=1,
            start_char=0,
            end_char=16,
        ),
        ChunkForEmbedding(
            chunk_index=1,
            content="Embedded chunk B",
            page_number=1,
            start_char=17,
            end_char=33,
        ),
    ]
    completed_point_ids = ["vec-a", "vec-b"]

    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(
        side_effect=EmbeddingBatchError(
            "batch 3/5 failed: connection reset",
            completed_chunks=completed_chunks,
            completed_point_ids=completed_point_ids,
        )
    )
    embedder = MagicMock()

    with pytest.raises(RuntimeError) as exc_info:
        await run_process_pdf(
            paper_id=77,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )

    # The completed chunks were persisted via _persist_chunk_rows ->
    # conn.executemany with the resumable rows (proves end-to-end wiring).
    conn.executemany.assert_awaited_once()
    persisted_rows = conn.executemany.await_args.args[1]
    assert [r[1] for r in persisted_rows] == [0, 1]  # chunk_index
    assert [r[0] for r in persisted_rows] == [77, 77]  # paper_id
    assert [r[6] for r in persisted_rows] == ["vec-a", "vec-b"]  # point_id
    # Still surfaced as a recoverable/retryable failure.
    message = str(exc_info.value)
    assert "2 chunks saved" in message
    assert "retry to resume" in message


@pytest.mark.asyncio
async def test_run_process_pdf_embedding_batch_error_with_no_completed_chunks_skips_persist():
    """When no batch succeeded, there is nothing to persist — the handler must
    not call executemany and still re-raises a recoverable RuntimeError."""
    from paper_ingestion.ingestion.embedder import EmbeddingBatchError

    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(
        side_effect=EmbeddingBatchError(
            "batch 1/5 failed immediately",
            completed_chunks=[],
            completed_point_ids=[],
        )
    )
    embedder = MagicMock()

    with pytest.raises(RuntimeError) as exc_info:
        await run_process_pdf(
            paper_id=78,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )

    conn.executemany.assert_not_awaited()
    assert "0 chunks saved" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ING-001: total_batches ceiling division
# ---------------------------------------------------------------------------


def test_total_batches_ceiling_division() -> None:
    """33 chunks with batch_size=32 → total_batches=2 (ceiling division)."""
    # Replicate the formula from pdf_workflow.run_process_pdf
    batch_size = 32
    for n_chunks, expected in [
        (0, 1),  # max(..., 1) guard
        (1, 1),
        (32, 1),
        (33, 2),
        (64, 2),
        (65, 3),
    ]:
        result = max((n_chunks + batch_size - 1) // batch_size, 1)
        assert result == expected, f"n_chunks={n_chunks}: got {result}, want {expected}"


@pytest.mark.asyncio
async def test_run_process_pdf_stores_chunks_and_returns_processed():
    """Successful processing writes chunks and returns processed status."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool = _make_pool(conn)

    chunks = [
        SimpleNamespace(
            chunk_index=0,
            content="Chunk A",
            page_number=1,
            start_char=0,
            end_char=7,
        ),
        SimpleNamespace(
            chunk_index=1,
            content="Chunk B",
            page_number=2,
            start_char=8,
            end_char=15,
        ),
    ]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", chunks, ["vec-a", "vec-b"]))
    embedder = MagicMock()

    result = await run_process_pdf(
        paper_id=12,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
    )

    assert result == {"paper_id": 12, "chunk_count": 2, "status": "processed"}
    conn.executemany.assert_awaited_once()
    inserted_rows = conn.executemany.await_args.args[1]
    assert inserted_rows[0][0] == 12
    assert inserted_rows[0][6] == "vec-a"
    assert inserted_rows[1][6] == "vec-b"


# ---------------------------------------------------------------------------
# W1.7-G: torch OOM / CUDA error differentiation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_workflow_relabels_torch_oom_as_distinct_error():
    """torch.OutOfMemoryError is re-raised with a GPU-specific actionable message."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=torch.OutOfMemoryError("simulated OOM"))
    embedder = MagicMock()

    with pytest.raises(RuntimeError, match="GPU out-of-memory"):
        await run_process_pdf(
            paper_id=42,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )


@pytest.mark.asyncio
async def test_pdf_workflow_relabels_cuda_runtime_error():
    """RuntimeError with 'CUDA out of memory' is re-raised with a GPU-specific message."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(
        side_effect=RuntimeError("CUDA out of memory: tried to allocate 2 GiB")
    )
    embedder = MagicMock()

    with pytest.raises(RuntimeError, match="GPU error"):
        await run_process_pdf(
            paper_id=43,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )


@pytest.mark.asyncio
async def test_pdf_workflow_preserves_embedding_error_for_httpx_failures():
    """httpx.HTTPStatusError is wrapped as 'Embedding service error'."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "503",
            request=httpx.Request("POST", "http://litellm/embed"),
            response=httpx.Response(503),
        )
    )
    embedder = MagicMock()

    with pytest.raises(RuntimeError, match="Embedding service error"):
        await run_process_pdf(
            paper_id=44,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )


@pytest.mark.parametrize("status_code", [400, 401, 500])
@pytest.mark.asyncio
async def test_pdf_workflow_embedding_http_status_stays_actionable(status_code: int):
    """Provider HTTP status survives PDF workflow wrapping while URLs are redacted."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool = _make_pool(conn)
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "http://litellm:4000/v1/embeddings"),
        text="provider detail",
    )
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            f"{status_code} at http://litellm:4000/v1/embeddings",
            request=response.request,
            response=response,
        )
    )
    embedder = MagicMock()

    with pytest.raises(RuntimeError) as exc_info:
        await run_process_pdf(
            paper_id=45,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )

    message = str(exc_info.value)
    assert f"{status_code}" in message
    assert "LITELLM_MASTER_KEY" in message
    assert "http://litellm:4000" not in message


# ---------------------------------------------------------------------------
# M-3: deterministic Qdrant point IDs — no orphans/duplicates on retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_process_pdf_retry_uses_same_point_ids_after_phase3_failure():
    """M-3 crash-safety: run_process_pdf threads point_ids from pdf_processor.process
    into DB rows unchanged, and does so consistently across two sequential calls for
    the same paper (simulating a retry after a Phase-3 / DB-insert failure).

    This test verifies the threading contract of run_process_pdf: whatever point IDs
    pdf_processor.process returns are persisted to the DB in the same order on every
    attempt.  It does NOT verify that embed_and_store computes deterministic uuid5 IDs
    — that guarantee is tested directly in test_embed_and_store_point_ids_are_deterministic.
    Together the two tests cover the full crash-safety property.
    """
    import uuid

    from paper_ingestion.ingestion.embedder import _CHUNK_POINT_ID_NAMESPACE

    chunks = [
        SimpleNamespace(
            chunk_index=0,
            content="Deterministic chunk A",
            page_number=1,
            start_char=0,
            end_char=21,
        ),
        SimpleNamespace(
            chunk_index=1,
            content="Deterministic chunk B",
            page_number=1,
            start_char=22,
            end_char=43,
        ),
    ]
    # Use the real uuid5 formula so the mock carries realistic IDs, but the
    # assertion below only checks stability across calls — not this computation.
    mock_ids = [
        str(uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, "55:0")),
        str(uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, "55:1")),
    ]

    def _make_fresh_pool_with_transaction():
        """Pool whose connection always reports 0 existing chunks (simulates retry state)."""
        conn = AsyncMock()
        conn.fetchval.return_value = 0  # no existing chunks (force or post-failure retry)
        conn.transaction = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=None),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        return _make_pool(conn), conn

    # --- First attempt ---
    pool1, conn1 = _make_fresh_pool_with_transaction()
    pdf_processor1 = MagicMock()
    pdf_processor1.process = AsyncMock(return_value=("full text", chunks, mock_ids))
    embedder1 = MagicMock()

    await run_process_pdf(
        paper_id=55,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool1,
        pdf_processor=pdf_processor1,
        embedder=embedder1,
    )

    first_call_rows = conn1.executemany.await_args.args[1]
    first_point_ids = [row[6] for row in first_call_rows]

    # --- Second attempt (retry after hypothetical Phase-3 failure) ---
    pool2, conn2 = _make_fresh_pool_with_transaction()
    pdf_processor2 = MagicMock()
    pdf_processor2.process = AsyncMock(return_value=("full text", chunks, mock_ids))
    embedder2 = MagicMock()

    await run_process_pdf(
        paper_id=55,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool2,
        pdf_processor=pdf_processor2,
        embedder=embedder2,
    )

    second_call_rows = conn2.executemany.await_args.args[1]
    second_point_ids = [row[6] for row in second_call_rows]

    # run_process_pdf must thread through point_ids from pdf_processor unchanged.
    # Stability across attempts is the invariant — both calls received the same
    # mock_ids so the DB rows must carry identical IDs on both attempts.
    assert first_point_ids == second_point_ids, (
        f"Point IDs diverged between attempts: {first_point_ids!r} vs {second_point_ids!r}"
    )
    assert len(first_point_ids) == len(chunks), "All chunks must have a point_id row"


@pytest.mark.asyncio
async def test_embed_and_store_point_ids_are_deterministic():
    """M-3 unit: embed_and_store derives point IDs from (paper_id, chunk_index).

    Two calls with the same paper_id and chunk indices must produce identical
    UUIDs, making Qdrant upsert idempotent across retries.
    """
    import uuid

    from paper_ingestion.ingestion.embedder import (
        _CHUNK_POINT_ID_NAMESPACE,
        EMBEDDING_DIMENSION,
        Embedder,
    )

    def _make_embedder_for_test() -> Embedder:
        http = MagicMock()
        qdrant = AsyncMock()
        e = Embedder(http, qdrant)
        return e

    chunks = [
        ChunkForEmbedding(
            chunk_index=0, content="chunk A", page_number=1, start_char=0, end_char=7
        ),
        ChunkForEmbedding(
            chunk_index=1, content="chunk B", page_number=1, start_char=8, end_char=15
        ),
    ]

    fake_embedding = [0.1] * EMBEDDING_DIMENSION

    embedder = _make_embedder_for_test()
    embedder.embed_texts = AsyncMock(return_value=[fake_embedding] * len(chunks))

    ids_first = await embedder.embed_and_store(paper_id=7, chunks=chunks)

    # Fresh mocks for the second call — same inputs must yield same IDs.
    embedder.embed_texts = AsyncMock(return_value=[fake_embedding] * len(chunks))
    embedder.qdrant = AsyncMock()

    ids_second = await embedder.embed_and_store(paper_id=7, chunks=chunks)

    assert ids_first == ids_second, (
        f"Point IDs are not deterministic: {ids_first!r} vs {ids_second!r}"
    )

    # Verify IDs match the expected deterministic formula.
    expected = [
        str(uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, "7:0")),
        str(uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, "7:1")),
    ]
    assert ids_first == expected


# ---------------------------------------------------------------------------
# DA-02: force-reprocess must not delete just-upserted vectors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_reprocess_preserves_overlapping_vectors():
    """DA-02: when old and new chunk sets share point IDs (same chunk index),
    the Qdrant delete must exclude the new IDs so just-upserted vectors survive.

    Scenario:
      old  = [vec-0, vec-1, vec-2]  (3 chunks, paper shrinks to 2)
      new  = [vec-0, vec-1]         (reprocess produces same IDs for indices 0+1)
      expected delete = {vec-2}     (only the stale trailing chunk)
    """
    conn = AsyncMock()
    conn.fetchval.return_value = 3  # existing_count > 0, triggers force path
    conn.fetch.return_value = [
        {"embedding_id": "vec-0"},
        {"embedding_id": "vec-1"},
        {"embedding_id": "vec-2"},
    ]
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool = _make_pool(conn)

    new_chunks = [
        SimpleNamespace(chunk_index=0, content="A", page_number=1, start_char=0, end_char=1),
        SimpleNamespace(chunk_index=1, content="B", page_number=1, start_char=2, end_char=3),
    ]
    new_point_ids = ["vec-0", "vec-1"]  # overlap with old[0] and old[1]

    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", new_chunks, new_point_ids))
    embedder = MagicMock()
    embedder.qdrant.delete = AsyncMock()

    result = await run_process_pdf(
        paper_id=99,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=True,
    )

    assert result == {"paper_id": 99, "chunk_count": 2, "status": "processed"}

    # Only vec-2 (stale) should be deleted; vec-0 and vec-1 must be preserved.
    embedder.qdrant.delete.assert_awaited_once()
    call_kwargs = embedder.qdrant.delete.await_args
    assert call_kwargs is not None
    deleted_ids = set(call_kwargs.kwargs["points_selector"].points)
    assert deleted_ids == {"vec-2"}, f"Expected only stale vec-2 deleted, got {deleted_ids}"


@pytest.mark.asyncio
async def test_force_reprocess_skips_qdrant_delete_when_new_fully_covers_old():
    """DA-02: when new chunk IDs fully cover all old IDs (no stale vectors),
    the Qdrant delete must not be called at all (empty difference → guarded by if)."""
    conn = AsyncMock()
    conn.fetchval.return_value = 2
    conn.fetch.return_value = [
        {"embedding_id": "vec-0"},
        {"embedding_id": "vec-1"},
    ]
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool = _make_pool(conn)

    new_chunks = [
        SimpleNamespace(chunk_index=0, content="A", page_number=1, start_char=0, end_char=1),
        SimpleNamespace(chunk_index=1, content="B", page_number=1, start_char=2, end_char=3),
    ]
    # Identical IDs → set difference is empty
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", new_chunks, ["vec-0", "vec-1"]))
    embedder = MagicMock()
    embedder.qdrant.delete = AsyncMock()

    result = await run_process_pdf(
        paper_id=100,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=True,
    )

    assert result == {"paper_id": 100, "chunk_count": 2, "status": "processed"}
    embedder.qdrant.delete.assert_not_awaited()
