"""Unit tests for shared PDF workflow helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import torch

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.services.pdf_workflow import advisory_lock, run_process_pdf


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
    pool, _ = make_pool_and_conn(conn=conn)
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
    pool, _ = make_pool_and_conn(conn=conn)
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

    assert result["paper_id"] == 5
    assert result["chunk_count"] == 1
    assert result["status"] == "processed"
    # M11a: the best-effort cleanup failure is surfaced in the payload, not silent.
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert "Stale-vector cleanup failed" in warnings[0]
    assert "1 stale vector(s)" in warnings[0]
    conn.execute.assert_any_await("DELETE FROM paper_chunks WHERE paper_id = $1", 5)
    conn.executemany.assert_awaited_once()
    embedder.qdrant.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_process_pdf_wraps_embedding_failures():
    """Embedding errors keep sanitized cause detail for operators."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool, _ = make_pool_and_conn(conn=conn)
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
    pool, _ = make_pool_and_conn(conn=conn)

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
    pool, _ = make_pool_and_conn(conn=conn)
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


@pytest.mark.asyncio
async def test_extraction_progress_zero_chunks_returns_0_4_without_division_error():
    """_extraction_progress zero-chunks guard: when total_chunks=0, progress
    must be mapped to 0.4 (frac=1.0 → 0.1 + 0.3*1.0) and no ZeroDivisionError
    must be raised.

    # Verified: services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:298-304
    # (_extraction_progress: if total_chunks > 0: frac = chunk_index/total_chunks
    #  else: frac = 1.0 → _maybe_progress(0.1 + 0.3*frac, ...) → 0.4)
    """
    captured_progress: list[float] = []

    ctx = MagicMock()
    ctx.update_progress = AsyncMock(side_effect=lambda p, msg=None: captured_progress.append(p))

    conn = AsyncMock()
    # First fetchval: existing_count → 0 (no short-circuit).
    # Second fetchval: owner_id → None.
    conn.fetchval.side_effect = [0, None]
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)

    async def _invoke_callback_with_zero_chunks(
        pdf_path, paper_id, *, user_id, progress_callback, resume_content=None
    ):
        # Simulate the extractor calling back once with zero total chunks.
        await progress_callback(chunk_index=0, total_chunks=0)
        return ("", [], [])

    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=_invoke_callback_with_zero_chunks)
    embedder = MagicMock()
    embedder.qdrant = MagicMock()

    await run_process_pdf(
        paper_id=42,
        pdf_path=Path("/tmp/zero-chunks.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        ctx=ctx,
    )

    zero_chunk_progress_calls = [p for p in captured_progress if abs(p - 0.4) < 1e-9]
    assert zero_chunk_progress_calls, (
        f"Expected _extraction_progress(0, 0) to emit progress=0.4; "
        f"captured progress calls: {captured_progress}"
    )


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
    pool, _ = make_pool_and_conn(conn=conn)

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
# torch OOM / CUDA error differentiation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_workflow_relabels_torch_oom_as_distinct_error():
    """torch.OutOfMemoryError is re-raised with a GPU-specific actionable message."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool, _ = make_pool_and_conn(conn=conn)
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
    pool, _ = make_pool_and_conn(conn=conn)
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
async def test_pdf_workflow_importable_and_error_paths_safe_without_torch():
    """H8: torch is an optional GPU dependency — the module must import with
    torch absent, and the error handlers must not explode at exception time
    (the old ``except torch.OutOfMemoryError`` clause raised when torch=None)."""
    import importlib
    import sys

    from paper_ingestion.services import pdf_workflow

    real_torch = sys.modules["torch"]
    # A None entry in sys.modules makes ``import torch`` raise ImportError.
    sys.modules["torch"] = None  # type: ignore[assignment]
    try:
        reloaded = importlib.reload(pdf_workflow)
        # Guarded import took the ImportError branch.
        assert reloaded.torch is None

        conn = AsyncMock()
        conn.fetchval.return_value = 0
        pool, _ = make_pool_and_conn(conn=conn)
        pdf_processor = MagicMock()
        pdf_processor.process = AsyncMock(
            side_effect=RuntimeError("CUDA out of memory: tried to allocate 2 GiB")
        )
        embedder = MagicMock()

        # The RuntimeError handler is entered (the path that previously
        # dereferenced torch.OutOfMemoryError); the string-match CUDA branch
        # still produces the GPU-specific message without exploding.
        with pytest.raises(RuntimeError, match="GPU error"):
            await reloaded.run_process_pdf(
                paper_id=46,
                pdf_path=Path("/tmp/paper.pdf"),
                db_pool=pool,
                pdf_processor=pdf_processor,
                embedder=embedder,
            )
    finally:
        sys.modules["torch"] = real_torch
        importlib.reload(pdf_workflow)


@pytest.mark.asyncio
async def test_pdf_workflow_preserves_embedding_error_for_httpx_failures():
    """httpx.HTTPStatusError is wrapped as 'Embedding service error'."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool, _ = make_pool_and_conn(conn=conn)
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


# ---------------------------------------------------------------------------
# CFG-PROGRESS-1: progress_callback threaded into pdf_processor.process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_callback_threaded_into_processor():
    """pdf_processor.process must receive a progress_callback when ctx is provided."""
    from types import SimpleNamespace

    conn = AsyncMock()
    conn.fetchval.return_value = 0  # no existing chunks → proceed to embedding step
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)

    chunks = [SimpleNamespace(chunk_index=0, content="c", page_number=1, start_char=0, end_char=1)]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("text", chunks, ["vec-1"]))
    embedder = MagicMock()

    progress_calls: list[float] = []

    class FakeCtx:
        async def update_progress(self, pct: float, msg: str | None = None) -> None:
            progress_calls.append(pct)

    await run_process_pdf(
        paper_id=100,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        ctx=FakeCtx(),
    )

    _, kwargs = pdf_processor.process.call_args
    assert "progress_callback" in kwargs, (
        "progress_callback must be forwarded to pdf_processor.process()"
    )
    callback = kwargs["progress_callback"]
    assert callable(callback), "progress_callback must be callable"


@pytest.mark.asyncio
async def test_extraction_progress_maps_to_01_04_range():
    """The extraction progress callback maps chunk progress to the 0.1-0.4 window."""
    from types import SimpleNamespace

    conn = AsyncMock()
    conn.fetchval.return_value = 0
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)

    chunks = [SimpleNamespace(chunk_index=0, content="c", page_number=1, start_char=0, end_char=1)]

    captured_callback: list = []

    async def capture_and_succeed(
        pdf_path, paper_id, *, user_id=None, progress_callback=None, resume_content=None
    ):
        if progress_callback is not None:
            captured_callback.append(progress_callback)
        return ("text", chunks, ["vec-1"])

    pdf_processor = MagicMock()
    pdf_processor.process = capture_and_succeed
    embedder = MagicMock()

    progress_calls: list[float] = []

    class FakeCtx:
        async def update_progress(self, pct: float, msg: str | None = None) -> None:
            progress_calls.append(pct)

    await run_process_pdf(
        paper_id=101,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        ctx=FakeCtx(),
    )

    assert len(captured_callback) == 1, "progress_callback was not captured"
    cb = captured_callback[0]

    # Simulate: chunk 0 of 4 → 0.1 + 0.3*(0/4) = 0.1
    await cb(0, 4)
    assert abs(progress_calls[-1] - 0.1) < 1e-9, f"Expected 0.1, got {progress_calls[-1]}"

    # chunk 2 of 4 → 0.1 + 0.3*(2/4) = 0.25
    await cb(2, 4)
    assert abs(progress_calls[-1] - 0.25) < 1e-9, f"Expected 0.25, got {progress_calls[-1]}"

    # chunk 4 of 4 → 0.1 + 0.3*(4/4) = 0.4
    await cb(4, 4)
    assert abs(progress_calls[-1] - 0.4) < 1e-9, f"Expected 0.4, got {progress_calls[-1]}"


@pytest.mark.parametrize("status_code", [400, 401, 500])
@pytest.mark.asyncio
async def test_pdf_workflow_embedding_http_status_stays_actionable(status_code: int):
    """Provider HTTP status survives PDF workflow wrapping while URLs are redacted."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool, _ = make_pool_and_conn(conn=conn)
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
        return make_pool_and_conn(conn=conn, with_transaction=False)[0], conn

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
    pool, _ = make_pool_and_conn(conn=conn)

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

    # T1.2: force=True bypasses the resume skip entirely (the resume query
    # never runs — force always re-embeds every chunk).
    assert pdf_processor.process.call_args.kwargs["resume_content"] == {}

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
    pool, _ = make_pool_and_conn(conn=conn)

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


# ---------------------------------------------------------------------------
# ING-1: re-embed papers stuck with an incomplete chunk set (chunked_at marker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_process_pdf_reembeds_when_chunked_at_unset():
    """A paper with chunk rows but chunked_at IS NULL (a prior partial embed)
    must NOT short-circuit: run_process_pdf must call the processor to finish
    embedding the missing chunks."""
    conn = AsyncMock()
    # fetchval order in run_process_pdf: existing_count, chunked_at, owner_id.
    conn.fetchval.side_effect = [3, None, None]  # 3 partial chunks, never marked complete
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    chunks = [
        SimpleNamespace(chunk_index=0, content="A", page_number=1, start_char=0, end_char=1),
    ]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", chunks, ["vec-a"]))
    embedder = MagicMock()

    result = await run_process_pdf(
        paper_id=5,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=False,
    )

    assert result["status"] == "processed"
    pdf_processor.process.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_process_pdf_short_circuits_when_chunked_at_set():
    """A fully-processed paper (chunked_at set) still short-circuits."""
    conn = AsyncMock()
    conn.fetchval.side_effect = [4, datetime(2026, 6, 17, tzinfo=UTC)]
    pool, _ = make_pool_and_conn(conn=conn)
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
async def test_run_process_pdf_marks_chunked_at_on_success():
    """A successful run stamps papers.chunked_at so future retries short-circuit."""
    conn = AsyncMock()
    conn.fetchval.side_effect = [0, None]  # no existing chunks, owner_id None
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    chunks = [
        SimpleNamespace(chunk_index=0, content="A", page_number=1, start_char=0, end_char=1),
    ]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", chunks, ["vec-a"]))
    embedder = MagicMock()

    await run_process_pdf(
        paper_id=12,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
    )

    conn.execute.assert_any_await("UPDATE papers SET chunked_at = now() WHERE id = $1", 12)


# ---------------------------------------------------------------------------
# T1.2: resume — skip already-embedded chunks on retry (content AND model
# identity); the resume map itself is built here, before pdf_processor.process
# is called, and threaded in as the resume_content kwarg.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_process_pdf_resume_excludes_other_model_rows():
    """Model-change regression: the resume query is scoped to
    embedding_model = EMBEDDING_MODEL_NAME.  A prior row embedded by an
    OLDER model is excluded from resume_content (forcing a re-embed) even
    though its content is unchanged; a same-model row with unchanged content
    IS included."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    # Fixture rows a real WHERE clause would hold: one embedded by an
    # obsolete model, one by the current model. The fake conn.fetch applies
    # the same `embedding_model = $2` filter a real DB would.
    fixture_rows = [
        {"chunk_index": 0, "content": "Stable content", "embedding_model": "obsolete-model"},
        {"chunk_index": 1, "content": "Also stable", "embedding_model": EMBEDDING_MODEL_NAME},
    ]

    async def _fetch(sql, *params):
        _paper_id, model = params
        return [
            {"chunk_index": r["chunk_index"], "content": r["content"]}
            for r in fixture_rows
            if r["embedding_model"] == model
        ]

    conn = AsyncMock()
    conn.fetchval.side_effect = [0, None]  # no existing chunks, owner_id None
    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)

    chunks = [
        SimpleNamespace(
            chunk_index=0, content="Stable content", page_number=1, start_char=0, end_char=14
        ),
        SimpleNamespace(
            chunk_index=1, content="Also stable", page_number=1, start_char=15, end_char=26
        ),
    ]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", chunks, ["vec-a", "vec-b"]))
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[SimpleNamespace(id=chunk_point_id(200, 1))])

    await run_process_pdf(
        paper_id=200,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=False,
    )

    _, kwargs = pdf_processor.process.call_args
    assert kwargs["resume_content"] == {1: "Also stable"}, (
        "obsolete-model row (index 0) must be excluded; same-model row (index 1) included"
    )


@pytest.mark.asyncio
async def test_run_process_pdf_resume_reembeds_when_qdrant_point_missing():
    """Vector-loss regression: a prior row with matching content AND model
    whose Qdrant point no longer exists (Qdrant's backup leg is best-effort
    per backup.sh — a restored DB can outlive its vectors) must be dropped
    from resume_content and re-embedded, not skipped forever."""

    async def _fetch(sql, *params):
        return [{"chunk_index": 0, "content": "Orphaned content"}]

    conn = AsyncMock()
    conn.fetchval.side_effect = [0, None]
    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)

    chunks = [
        SimpleNamespace(
            chunk_index=0, content="Orphaned content", page_number=1, start_char=0, end_char=16
        ),
    ]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", chunks, ["vec-a"]))
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[])  # point absent from Qdrant

    await run_process_pdf(
        paper_id=201,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=False,
    )

    embedder.qdrant.retrieve.assert_awaited_once()
    _, kwargs = pdf_processor.process.call_args
    assert kwargs["resume_content"] == {}, "a row whose Qdrant point is absent must re-embed"
