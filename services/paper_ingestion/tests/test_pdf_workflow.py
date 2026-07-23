"""Unit tests for shared PDF workflow helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import torch

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.ingestion.payload_schema import VectorVisibility
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.services import pdf_workflow as pdf_workflow_module
from paper_ingestion.services.pdf_workflow import (
    advisory_lock,
    download_and_store_pdf,
    reconcile_paper_embeddings,
    run_process_pdf,
)

_TEST_VISIBILITY_GENERATION = "a" * 32
_TEST_VECTOR_VISIBILITY = VectorVisibility(
    source_type="arxiv",
    visibility_scope="public",
    visibility_generation=_TEST_VISIBILITY_GENERATION,
)


@pytest.fixture(autouse=True)
def _default_vector_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep workflow tests focused while production generation plumbing is isolated."""
    monkeypatch.setattr(
        pdf_workflow_module,
        "_resolve_visibility_generation",
        AsyncMock(return_value=_TEST_VISIBILITY_GENERATION),
    )
    monkeypatch.setattr(
        pdf_workflow_module,
        "_load_paper_embedding_context",
        AsyncMock(return_value=(_TEST_VECTOR_VISIBILITY, 17)),
    )


@pytest.mark.asyncio
async def test_download_and_store_pdf_commits_file_and_database_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The numeric PDF becomes durable only with its database update."""
    staged = tmp_path / "_download_12.pdf"
    final = tmp_path / "12.pdf"
    staged.write_bytes(b"%PDF-1.7\nnew")
    processor = MagicMock()
    processor.stage_pdf_download = AsyncMock(return_value=(staged, final))
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 12, "pdf_local_path": str(final)})
    pool, _ = make_pool_and_conn(conn=conn)
    monkeypatch.setattr(
        "paper_ingestion.pdf_processor.maintenance_active",
        lambda: False,
    )

    row = await download_and_store_pdf(pool, processor, "https://example.test/12.pdf", 12)

    assert row["id"] == 12
    assert final.read_bytes() == b"%PDF-1.7\nnew"
    assert not staged.exists()
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_and_store_pdf_restores_prior_file_when_db_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed DB commit cannot leave request bytes at the numeric path."""
    staged = tmp_path / "_download_13.pdf"
    final = tmp_path / "13.pdf"
    staged.write_bytes(b"%PDF-1.7\nnew")
    final.write_bytes(b"%PDF-1.7\nold")
    processor = MagicMock()
    processor.stage_pdf_download = AsyncMock(return_value=(staged, final))
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 13})
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(side_effect=RuntimeError("database commit failed"))
    conn.transaction = MagicMock(return_value=transaction)
    pool, _ = make_pool_and_conn(conn=conn, with_transaction=False)
    monkeypatch.setattr(
        "paper_ingestion.pdf_processor.maintenance_active",
        lambda: False,
    )

    with pytest.raises(RuntimeError, match="database commit failed"):
        await download_and_store_pdf(pool, processor, "https://example.test/13.pdf", 13)

    assert final.read_bytes() == b"%PDF-1.7\nold"
    assert not staged.exists()


@pytest.mark.asyncio
async def test_advisory_lock_unlocks_even_after_error():
    """advisory_lock always releases the PostgreSQL advisory lock."""
    conn = AsyncMock()

    with pytest.raises(RuntimeError, match="boom"):
        async with advisory_lock(conn, 3, 7):
            raise RuntimeError("boom")

    assert conn.execute.await_args_list[0].args == ("SELECT pg_advisory_lock($1, $2)", 3, 7)
    assert conn.execute.await_args_list[1].args == ("SELECT pg_advisory_unlock($1, $2)", 3, 7)


class _SingleSlotProbePool:
    """One-slot pool fake that records whether a waiter retains the slot."""

    def __init__(self, *, lock_available: bool) -> None:
        self.lock_available = lock_available
        self.slot = asyncio.Semaphore(1)
        self.probe_released = asyncio.Event()
        self.in_use = 0
        self.unlock_calls: list[tuple[object, ...]] = []

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                await pool.slot.acquire()
                pool.in_use += 1
                return self

            async def __aexit__(self, *_args):
                pool.in_use -= 1
                pool.slot.release()
                pool.probe_released.set()
                return False

            async def fetchrow(self, _statement, *_args):
                return {"acquired": pool.lock_available}

            async def execute(self, _statement, *args):
                pool.unlock_calls.append(args)

        return _Acquire()


@pytest.mark.asyncio
async def test_paper_lock_waiter_releases_single_pool_slot_between_probes():
    """A contended paper lock cannot monopolize the pool's only connection."""
    pool = _SingleSlotProbePool(lock_available=False)
    entered = asyncio.Event()

    async def _waiter():
        async with pdf_workflow_module._paper_mutation_connection(  # type: ignore[attr-defined]
            pool,
            77,  # type: ignore[arg-type]
        ):
            entered.set()

    waiter = asyncio.create_task(_waiter())
    await asyncio.wait_for(pool.probe_released.wait(), timeout=1)

    async with asyncio.timeout(0.1):
        async with pool.acquire():
            assert pool.in_use == 1

    assert not entered.is_set()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pool.in_use == 0
    assert pool.unlock_calls == []


@pytest.mark.asyncio
async def test_paper_lock_cancellation_unlocks_and_releases_connection():
    """Cancelling a lock holder releases both PostgreSQL lock and pool slot."""
    pool = _SingleSlotProbePool(lock_available=True)
    entered = asyncio.Event()

    async def _holder():
        async with pdf_workflow_module._paper_mutation_connection(  # type: ignore[attr-defined]
            pool,
            77,  # type: ignore[arg-type]
        ):
            entered.set()
            await asyncio.Event().wait()

    holder = asyncio.create_task(_holder())
    await asyncio.wait_for(entered.wait(), timeout=1)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    assert pool.unlock_calls == [(1, 77)]
    assert pool.in_use == 0
    async with asyncio.timeout(0.1):
        async with pool.acquire():
            assert pool.in_use == 1


@pytest.mark.asyncio
async def test_run_process_pdf_returns_already_processed_without_force():
    """Healthy persisted chunks reconcile without extracting the PDF."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    conn = AsyncMock()
    conn.fetchval.return_value = 4
    rows = [
        _persisted_chunk(
            5,
            index,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=chunk_point_id(5, index),
        )
        for index in range(4)
    ]
    conn.fetch.return_value = rows
    pool, _ = make_pool_and_conn(conn=conn)
    pdf_processor = MagicMock()
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[_vector_record_for_row(row) for row in rows])

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
        reloaded._resolve_visibility_generation = AsyncMock(  # type: ignore[attr-defined]
            return_value=_TEST_VISIBILITY_GENERATION
        )
        reloaded._load_paper_embedding_context = AsyncMock(  # type: ignore[attr-defined]
            return_value=(_TEST_VECTOR_VISIBILITY, 17)
        )

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
# Truthful PDF progress reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_process_pdf_maps_real_events_monotonically_after_commit():
    """Workflow progress follows real phase events and database durability."""
    timeline: list[str] = []
    progress_events: list[tuple[float, str | None]] = []
    conn = AsyncMock()
    conn.fetchval.side_effect = [0, None]
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)

    async def _commit(*_args) -> bool:
        timeline.append("database committed")
        return False

    transaction.__aexit__ = AsyncMock(side_effect=_commit)
    conn.transaction = MagicMock(return_value=transaction)
    pool, _ = make_pool_and_conn(conn=conn, with_transaction=False)

    chunks = [SimpleNamespace(chunk_index=0, content="c", page_number=1, start_char=0, end_char=1)]

    async def _process(
        _pdf_path,
        _paper_id,
        *,
        user_id=None,
        visibility=None,
        progress_callback=None,
        resume_content=None,
    ):
        assert progress_callback is not None
        await progress_callback("extracted", 1, 1)
        await progress_callback("chunked", 1, 1)
        await progress_callback("embedding", 1, 2)
        await progress_callback("embedding", 2, 2)
        return "text", chunks, ["vec-1"]

    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=_process)
    embedder = MagicMock()

    class FakeCtx:
        async def update_progress(self, pct: float, msg: str | None = None) -> None:
            progress_events.append((pct, msg))
            timeline.append(f"progress:{pct}:{msg}")

    await run_process_pdf(
        paper_id=100,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        ctx=FakeCtx(),
    )

    assert progress_events == [
        (0.1, "Downloaded"),
        (0.3, "Extracted"),
        (0.5, "Chunked"),
        (0.7, "Embedding batch 1/2"),
        (0.9, "Embedding batch 2/2"),
        (0.95, "Saved chunks"),
        (1.0, "Done"),
    ]
    assert [progress for progress, _message in progress_events] == sorted(
        progress for progress, _message in progress_events
    )
    assert timeline.index("database committed") < timeline.index("progress:0.95:Saved chunks")


@pytest.mark.asyncio
async def test_run_process_pdf_database_failure_never_reports_saved_or_done():
    """Failed PostgreSQL persistence cannot advance progress to saved or complete."""
    progress_events: list[tuple[float, str | None]] = []
    conn = AsyncMock()
    conn.fetchval.side_effect = [0, None]
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(side_effect=RuntimeError("database commit failed"))
    conn.transaction = MagicMock(return_value=transaction)
    pool, _ = make_pool_and_conn(conn=conn, with_transaction=False)

    chunks = [SimpleNamespace(chunk_index=0, content="c", page_number=1, start_char=0, end_char=1)]

    async def _process(
        _pdf_path,
        _paper_id,
        *,
        user_id=None,
        visibility=None,
        progress_callback=None,
        resume_content=None,
    ):
        assert progress_callback is not None
        await progress_callback("extracted", 1, 1)
        await progress_callback("chunked", 1, 1)
        await progress_callback("embedding", 1, 1)
        return "text", chunks, ["vec-1"]

    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=_process)
    embedder = MagicMock()

    class FakeCtx:
        async def update_progress(self, pct: float, msg: str | None = None) -> None:
            progress_events.append((pct, msg))

    with pytest.raises(RuntimeError, match="database commit failed"):
        await run_process_pdf(
            paper_id=101,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
            ctx=FakeCtx(),
        )

    assert progress_events[-1] == (0.9, "Embedding batch 1/1")
    assert all(message not in {"Saved chunks", "Done"} for _progress, message in progress_events)


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

    # force=True bypasses the resume skip entirely (the resume query
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
    """A fully-processed paper with healthy vectors does not re-extract its PDF."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    conn = AsyncMock()
    conn.fetchval.side_effect = [4, datetime(2026, 6, 17, tzinfo=UTC)]
    rows = [
        _persisted_chunk(
            5,
            index,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=chunk_point_id(5, index),
        )
        for index in range(4)
    ]
    conn.fetch.return_value = rows
    pool, _ = make_pool_and_conn(conn=conn)
    pdf_processor = MagicMock()
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[_vector_record_for_row(row) for row in rows])

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


def _persisted_chunk(
    paper_id: int,
    chunk_index: int,
    *,
    model: str,
    embedding_id: str | None,
    content: str | None = None,
    discovered_by: int | None = 17,
    source_type: str = "arxiv",
    visibility_scope: str = "public",
) -> dict[str, object]:
    return {
        "paper_id": paper_id,
        "chunk_index": chunk_index,
        "content": content or f"Persisted chunk {chunk_index}",
        "page_number": 1,
        "start_char": chunk_index * 20,
        "end_char": chunk_index * 20 + 19,
        "embedding_id": embedding_id,
        "embedding_model": model,
        "source_type": source_type,
        "visibility_scope": visibility_scope,
        "discovered_by": discovered_by,
    }


def _vector_record_for_row(row: dict[str, object]) -> SimpleNamespace:
    from paper_ingestion.ingestion.embed_store import (
        chunk_embedding_fingerprint,
        chunk_point_id,
    )
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    paper_id = int(row["paper_id"])
    chunk_index = int(row["chunk_index"])
    content = str(row["content"])
    return SimpleNamespace(
        id=chunk_point_id(paper_id, chunk_index),
        payload={
            "paper_id": paper_id,
            "chunk_index": chunk_index,
            "embedding_model": EMBEDDING_MODEL_NAME,
            "embedding_fingerprint": chunk_embedding_fingerprint(content),
            "user_id": row["discovered_by"],
            "source_type": row["source_type"],
            "visibility_scope": row["visibility_scope"],
            "visibility_generation": _TEST_VISIBILITY_GENERATION,
        },
    )


@pytest.mark.asyncio
async def test_reconcile_changed_model_reembeds_persisted_chunks_without_pdf_extraction():
    """A model change repairs from DB chunks and commits metadata only after vector success."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    rows = [_persisted_chunk(301, 0, model="old-model", embedding_id="legacy-point")]
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[rows, rows])
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    expected_id = chunk_point_id(301, 0)
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[])
    embedder.embed_and_store = AsyncMock(return_value=[expected_id])

    result = await reconcile_paper_embeddings(301, pool, embedder)

    assert result == {"paper_id": 301, "chunk_count": 1, "status": "repaired"}
    embedder.embed_and_store.assert_awaited_once()
    args = embedder.embed_and_store.await_args
    assert args.args[0] == 301
    assert [chunk.content for chunk in args.args[1]] == ["Persisted chunk 0"]
    assert args.kwargs["user_id"] == 17
    assert args.kwargs["run_context"].resume_content == {}
    conn.executemany.assert_awaited_once()
    update_rows = conn.executemany.await_args.args[1]
    assert update_rows == [(301, 0, expected_id, EMBEDDING_MODEL_NAME)]


@pytest.mark.asyncio
async def test_reconcile_missing_deterministic_point_repairs_same_model_row():
    """A DB-complete row is stale when its deterministic Qdrant point is absent."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    expected_id = chunk_point_id(302, 0)
    rows = [
        _persisted_chunk(
            302,
            0,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=expected_id,
        )
    ]
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[rows, rows])
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[])
    embedder.embed_and_store = AsyncMock(return_value=[expected_id])

    result = await reconcile_paper_embeddings(302, pool, embedder)

    assert result["status"] == "repaired"
    embedder.embed_and_store.assert_awaited_once()
    assert embedder.embed_and_store.await_args.kwargs["run_context"].resume_content == {}


@pytest.mark.asyncio
async def test_reconcile_repairs_visibility_payload_without_reembedding() -> None:
    """Authorization-only drift preserves the deterministic vector and fingerprint."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    expected_id = chunk_point_id(312, 0)
    row = _persisted_chunk(
        312,
        0,
        model=EMBEDDING_MODEL_NAME,
        embedding_id=expected_id,
    )
    record = _vector_record_for_row(row)
    record.payload["visibility_generation"] = "b" * 32
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[row])
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[record])
    embedder.qdrant.set_payload = AsyncMock()
    embedder.embed_and_store = AsyncMock()

    result = await reconcile_paper_embeddings(312, pool, embedder)

    assert result["status"] == "healthy"
    embedder.embed_and_store.assert_not_awaited()
    embedder.qdrant.set_payload.assert_awaited_once_with(
        collection_name="paper_chunks",
        payload=_TEST_VECTOR_VISIBILITY.payload,
        points=pdf_workflow_module.PointIdsList(points=[expected_id]),
        wait=True,
    )


@pytest.mark.asyncio
async def test_reconcile_worker_lost_lease_aborts_before_payload_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotated generation fences a stale worker while it holds the paper lock."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME
    from paper_ingestion.ingestion.payload_schema import StaleVisibilityLeaseError

    expected_id = chunk_point_id(313, 0)
    row = _persisted_chunk(
        313,
        0,
        model=EMBEDDING_MODEL_NAME,
        embedding_id=expected_id,
    )
    record = _vector_record_for_row(row)
    record.payload.pop("visibility_generation")
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[row])
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[record])
    embedder.qdrant.set_payload = AsyncMock()
    embedder.embed_and_store = AsyncMock()
    monkeypatch.setattr(
        pdf_workflow_module,
        "visibility_lease_is_current",
        AsyncMock(return_value=False),
    )

    with pytest.raises(StaleVisibilityLeaseError):
        await reconcile_paper_embeddings(
            313,
            pool,
            embedder,
            visibility_generation=_TEST_VISIBILITY_GENERATION,
            worker_lease_token="worker-a",
        )

    embedder.qdrant.set_payload.assert_not_awaited()
    embedder.embed_and_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_cleanup_preserves_same_content_from_new_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A point replaced just after the lease check survives stale cleanup."""
    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion.ingestion.embed_store import (
        chunk_embedding_fingerprint,
        chunk_point_id,
    )
    from qdrant_client.models import Distance, PointStruct, VectorParams

    qdrant = FauxQdrantClient()
    await qdrant.create_collection(
        collection_name="paper_chunks",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    paper_id = 313
    content = "same content across a visibility rotation"
    chunk = ChunkForEmbedding(
        chunk_index=0,
        content=content,
        page_number=1,
        start_char=0,
        end_char=len(content),
    )
    point_id = chunk_point_id(paper_id, chunk.chunk_index)
    fingerprint = chunk_embedding_fingerprint(content)
    old_generation = "a" * 32
    new_generation = "b" * 32
    await qdrant.upsert(
        collection_name="paper_chunks",
        points=[
            PointStruct(
                id=point_id,
                vector=[1.0, 0.0],
                payload={
                    "paper_id": paper_id,
                    "chunk_index": 0,
                    "embedding_fingerprint": fingerprint,
                    "visibility_generation": old_generation,
                },
            )
        ],
    )

    async def _replace_then_confirm_lease(*_args: object, **_kwargs: object) -> bool:
        await qdrant.upsert(
            collection_name="paper_chunks",
            points=[
                PointStruct(
                    id=point_id,
                    vector=[0.0, 1.0],
                    payload={
                        "paper_id": paper_id,
                        "chunk_index": 0,
                        "embedding_fingerprint": fingerprint,
                        "visibility_generation": new_generation,
                    },
                )
            ],
        )
        return True

    monkeypatch.setattr(
        pdf_workflow_module,
        "visibility_lease_is_current",
        _replace_then_confirm_lease,
    )
    await pdf_workflow_module._delete_reconcile_generation(
        SimpleNamespace(qdrant=qdrant),
        paper_id,
        [chunk],
        conn=AsyncMock(),
        visibility_generation=old_generation,
        worker_lease_token="old-worker",
    )

    points, _ = await qdrant.scroll(
        collection_name="paper_chunks",
        with_payload=True,
        with_vectors=True,
    )
    assert [point.id for point in points] == [point_id]
    assert points[0].payload["visibility_generation"] == new_generation
    assert points[0].vector == [0.0, 1.0]


@pytest.mark.asyncio
async def test_reconcile_ignores_legacy_vector_owner_for_authorization():
    """A legacy audit owner cannot force or prevent vector authorization."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    expected_id = chunk_point_id(311, 0)
    row = _persisted_chunk(
        311,
        0,
        model=EMBEDDING_MODEL_NAME,
        embedding_id=expected_id,
    )
    record = _vector_record_for_row(row)
    record.payload["user_id"] = 999
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[[row], [row]])
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[record])
    embedder.embed_and_store = AsyncMock(return_value=[expected_id])

    result = await reconcile_paper_embeddings(311, pool, embedder)

    assert result["status"] == "healthy"
    embedder.embed_and_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_healthy_vectors_is_bounded_noop():
    """Healthy metadata and vectors are probed in bounded batches and never re-embedded."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    rows = [
        _persisted_chunk(
            303,
            index,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=chunk_point_id(303, index),
        )
        for index in range(129)
    ]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()

    async def _retrieve(*, ids, **_kwargs):
        assert len(ids) <= 128
        rows_by_id = {str(_vector_record_for_row(row).id): row for row in rows}
        return [_vector_record_for_row(rows_by_id[str(point_id)]) for point_id in ids]

    embedder.qdrant.retrieve = AsyncMock(side_effect=_retrieve)
    embedder.embed_and_store = AsyncMock()

    result = await reconcile_paper_embeddings(303, pool, embedder)

    assert result == {"paper_id": 303, "chunk_count": 129, "status": "healthy"}
    assert embedder.qdrant.retrieve.await_count == 2
    embedder.embed_and_store.assert_not_awaited()
    conn.executemany.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_health_probe_retries_concurrent_chunk_change():
    """A vector matching the old DB snapshot cannot certify newer chunk content."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    old_row = _persisted_chunk(
        309,
        0,
        model=EMBEDDING_MODEL_NAME,
        embedding_id=chunk_point_id(309, 0),
        content="old content",
    )
    new_row = _persisted_chunk(
        309,
        0,
        model=EMBEDDING_MODEL_NAME,
        embedding_id=chunk_point_id(309, 0),
        content="new content",
    )
    current_rows = [old_row]
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=lambda *_args: list(current_rows))
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()

    async def _retrieve(**_kwargs):
        current_rows[:] = [new_row]
        return [_vector_record_for_row(old_row)]

    embedder.qdrant.retrieve = AsyncMock(side_effect=_retrieve)
    embedder.embed_and_store = AsyncMock(return_value=[chunk_point_id(309, 0)])

    result = await reconcile_paper_embeddings(309, pool, embedder)

    assert result["status"] == "repaired"
    embedder.embed_and_store.assert_awaited_once()

    assert any(
        call.args and str(call.args[0]).startswith("UPDATE papers SET chunked_at")
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_reconcile_probe_failure_is_retryable_and_does_not_mutate_metadata():
    """An unavailable Qdrant probe stays visible and cannot be mistaken for healthy state."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    rows = [
        _persisted_chunk(
            304,
            0,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=chunk_point_id(304, 0),
        )
    ]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(side_effect=RuntimeError("qdrant unavailable"))
    embedder.embed_and_store = AsyncMock()

    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        await reconcile_paper_embeddings(304, pool, embedder)

    embedder.embed_and_store.assert_not_awaited()
    conn.executemany.assert_not_awaited()


def _missing_qdrant_collection_error():
    from qdrant_client.http.exceptions import UnexpectedResponse

    return UnexpectedResponse(
        status_code=404,
        reason_phrase="Not Found",
        content=b'{"status":{"error":"Not found: Collection `paper_chunks` does not exist"}}',
        headers=httpx.Headers(),
    )


@pytest.mark.asyncio
async def test_reconcile_recreates_missing_collection_and_retries_probe_once():
    """The real qdrant-client 404 shape triggers collection setup and one retry."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id

    rows = [_persisted_chunk(307, 0, model="old-model", embedding_id="old-point")]
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[rows, rows])
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    expected_id = chunk_point_id(307, 0)
    embedder = MagicMock()
    embedder._collection_ensured = True
    embedder.ensure_collection = AsyncMock()
    embedder.qdrant.retrieve = AsyncMock(side_effect=[_missing_qdrant_collection_error(), []])
    embedder.embed_and_store = AsyncMock(return_value=[expected_id])

    result = await reconcile_paper_embeddings(307, pool, embedder)

    assert result["status"] == "repaired"
    assert embedder._collection_ensured is False
    embedder.ensure_collection.assert_awaited_once()
    assert embedder.qdrant.retrieve.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_missing_collection_retry_is_bounded():
    """A collection still missing after setup propagates after exactly one retry."""
    from qdrant_client.http.exceptions import UnexpectedResponse

    rows = [_persisted_chunk(308, 0, model="old-model", embedding_id="old-point")]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()
    embedder._collection_ensured = True
    embedder.ensure_collection = AsyncMock()
    embedder.qdrant.retrieve = AsyncMock(
        side_effect=[
            _missing_qdrant_collection_error(),
            _missing_qdrant_collection_error(),
        ]
    )

    with pytest.raises(UnexpectedResponse) as exc_info:
        await reconcile_paper_embeddings(308, pool, embedder)

    assert exc_info.value.status_code == 404
    embedder.ensure_collection.assert_awaited_once()
    assert embedder.qdrant.retrieve.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_embed_failure_keeps_old_model_metadata_for_retry():
    """Failed vector storage cannot advertise the active model in PostgreSQL."""
    rows = [_persisted_chunk(305, 0, model="old-model", embedding_id="old-point")]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool, _ = make_pool_and_conn(conn=conn)
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(return_value=[])
    embedder.embed_and_store = AsyncMock(side_effect=RuntimeError("embedding unavailable"))

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        await reconcile_paper_embeddings(305, pool, embedder)

    conn.executemany.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_stale_writer_repairs_before_returning():
    """A stale upsert is removed and retried before search can observe it."""
    from paper_ingestion.ingestion.embed_store import (
        chunk_embedding_fingerprint,
        chunk_point_id,
    )
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    paper_id = 306
    point_id = chunk_point_id(paper_id, 0)
    old_rows = [
        _persisted_chunk(
            paper_id,
            0,
            model="old-model",
            embedding_id="old-point",
            content="old content",
        )
    ]
    new_rows = [
        _persisted_chunk(
            paper_id,
            0,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=point_id,
            content="new content",
        )
    ]
    current_rows = list(old_rows)
    vector_payloads: dict[str, dict[str, object]] = {}

    conn = AsyncMock()

    async def _fetch(*_args, **_kwargs):
        return list(current_rows)

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)

    stale_upsert_ready = asyncio.Event()
    newer_write_committed = asyncio.Event()
    embed_calls = 0

    async def _embed_and_store(_paper_id, chunks, **_kwargs):
        nonlocal embed_calls
        embed_calls += 1
        if embed_calls == 1:
            stale_upsert_ready.set()
            await newer_write_committed.wait()
        for chunk in chunks:
            vector_payloads[point_id] = {
                "paper_id": paper_id,
                "chunk_index": chunk.chunk_index,
                "embedding_model": EMBEDDING_MODEL_NAME,
                "embedding_fingerprint": chunk_embedding_fingerprint(chunk.content),
                "user_id": 17,
                **_TEST_VECTOR_VISIBILITY.payload,
            }
        return [point_id]

    async def _retrieve(*, ids, **_kwargs):
        return [
            SimpleNamespace(id=record_id, payload=vector_payloads[record_id])
            for record_id in ids
            if record_id in vector_payloads
        ]

    async def _delete(*, points_selector, **_kwargs):
        for branch in points_selector.filter.should:
            point_ids = next(
                condition.has_id for condition in branch.must if getattr(condition, "has_id", None)
            )
            fingerprint = next(
                condition.match.value
                for condition in branch.must
                if getattr(condition, "key", None) == "embedding_fingerprint"
            )
            for record_id in point_ids:
                payload = vector_payloads.get(str(record_id))
                if payload and payload.get("embedding_fingerprint") == fingerprint:
                    vector_payloads.pop(str(record_id))

    embedder = MagicMock()
    embedder.embed_and_store = AsyncMock(side_effect=_embed_and_store)
    embedder.qdrant.retrieve = AsyncMock(side_effect=_retrieve)
    embedder.qdrant.delete = AsyncMock(side_effect=_delete)

    stale_reconcile = asyncio.create_task(reconcile_paper_embeddings(paper_id, pool, embedder))
    await stale_upsert_ready.wait()
    current_rows[:] = new_rows
    vector_payloads[point_id] = {
        "paper_id": paper_id,
        "chunk_index": 0,
        "embedding_model": EMBEDDING_MODEL_NAME,
        "embedding_fingerprint": chunk_embedding_fingerprint("new content"),
        "user_id": 17,
        **_TEST_VECTOR_VISIBILITY.payload,
    }
    newer_write_committed.set()

    repaired = await stale_reconcile
    assert repaired["status"] == "repaired"
    assert vector_payloads[point_id]["embedding_fingerprint"] == (
        chunk_embedding_fingerprint("new content")
    )
    embedder.qdrant.delete.assert_awaited_once()

    healthy = await reconcile_paper_embeddings(paper_id, pool, embedder)
    assert healthy["status"] == "healthy"
    assert embed_calls == 2


@pytest.mark.asyncio
async def test_concurrent_pdf_processing_cannot_publish_stale_same_id_vector():
    """Two first-time writers serialize the Qdrant write with the DB generation."""
    from paper_ingestion.ingestion.embed_store import (
        chunk_embedding_fingerprint,
        chunk_point_id,
    )
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    paper_id = 410
    point_id = chunk_point_id(paper_id, 0)
    paper_lock = asyncio.Lock()
    rows: dict[int, dict[str, object]] = {}
    chunked_at: datetime | None = None
    vector_payload: dict[str, object] | None = None

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Conn:
        def transaction(self):
            return _Transaction()

        async def fetchrow(self, sql, *_args):
            if "pg_try_advisory_lock" not in sql:
                raise AssertionError(sql)
            if paper_lock.locked():
                return {"acquired": False}
            await paper_lock.acquire()
            return {"acquired": True}

        async def execute(self, sql, *args):
            nonlocal chunked_at
            if "pg_advisory_unlock" in sql:
                paper_lock.release()
            elif sql.startswith("UPDATE papers SET chunked_at"):
                chunked_at = datetime.now(UTC)
            elif sql.startswith("DELETE FROM paper_chunks"):
                rows.clear()

        async def fetchval(self, sql, *_args):
            if "COUNT(*)" in sql:
                return len(rows)
            if "chunked_at" in sql:
                return chunked_at
            if "discovered_by" in sql:
                return 17
            raise AssertionError(sql)

        async def fetch(self, sql, *_args):
            if "FROM paper_chunks c" in sql:
                return [dict(row) for row in rows.values()]
            if "SELECT chunk_index, content" in sql:
                return [
                    {"chunk_index": row["chunk_index"], "content": row["content"]}
                    for row in rows.values()
                ]
            if "SELECT embedding_id" in sql:
                return [{"embedding_id": row["embedding_id"]} for row in rows.values()]
            raise AssertionError(sql)

        async def executemany(self, sql, values):
            if "INSERT INTO paper_chunks" in sql:
                for value in values:
                    rows.setdefault(
                        int(value[1]),
                        {
                            "paper_id": int(value[0]),
                            "chunk_index": int(value[1]),
                            "content": str(value[2]),
                            "page_number": value[3],
                            "start_char": value[4],
                            "end_char": value[5],
                            "embedding_id": value[6],
                            "embedding_model": value[7],
                            "source_type": "arxiv",
                            "visibility_scope": "public",
                            "discovered_by": 17,
                        },
                    )
                return
            raise AssertionError(sql)

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_args):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    first_started = asyncio.Event()
    first_may_write = asyncio.Event()
    second_finished_write = asyncio.Event()

    def _processor(content: str, *, pause: bool):
        processor = MagicMock()

        async def _process(*_args, **_kwargs):
            nonlocal vector_payload
            if pause:
                first_started.set()
                await first_may_write.wait()
            vector_payload = {
                "paper_id": paper_id,
                "chunk_index": 0,
                "embedding_model": EMBEDDING_MODEL_NAME,
                "embedding_fingerprint": chunk_embedding_fingerprint(content),
                "user_id": 17,
                **_TEST_VECTOR_VISIBILITY.payload,
            }
            if not pause:
                second_finished_write.set()
            chunk = SimpleNamespace(
                chunk_index=0,
                content=content,
                page_number=1,
                start_char=0,
                end_char=len(content),
            )
            return content, [chunk], [point_id]

        processor.process = AsyncMock(side_effect=_process)
        return processor

    embedder = MagicMock()

    async def _retrieve(**_kwargs):
        if vector_payload is None:
            return []
        return [SimpleNamespace(id=point_id, payload=dict(vector_payload))]

    embedder.qdrant.retrieve = AsyncMock(side_effect=_retrieve)
    first = asyncio.create_task(
        run_process_pdf(
            paper_id,
            Path("/tmp/first.pdf"),
            _Pool(),  # type: ignore[arg-type]
            _processor("generation A", pause=True),
            embedder,
        )
    )
    await first_started.wait()
    second_processor = _processor("generation B", pause=False)
    second = asyncio.create_task(
        run_process_pdf(
            paper_id,
            Path("/tmp/second.pdf"),
            _Pool(),  # type: ignore[arg-type]
            second_processor,
            embedder,
        )
    )

    try:
        await asyncio.wait_for(second_finished_write.wait(), timeout=0.1)
    except TimeoutError:
        pass
    first_may_write.set()
    await asyncio.gather(first, second)

    persisted = rows[0]
    assert vector_payload is not None
    assert vector_payload["embedding_fingerprint"] == chunk_embedding_fingerprint(
        str(persisted["content"])
    )
    if str(persisted["content"]) == "generation A":
        second_processor.process.assert_not_awaited()


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
# Resume: skip already-embedded chunks on retry (content AND model
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

    # The reconciliation query loads both rows so model/content/payload
    # identity can be checked in one place.
    fixture_rows = [
        _persisted_chunk(
            200,
            0,
            model="obsolete-model",
            embedding_id=chunk_point_id(200, 0),
            content="Stable content",
        ),
        _persisted_chunk(
            200,
            1,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=chunk_point_id(200, 1),
            content="Also stable",
        ),
    ]

    async def _fetch(_sql, *_params):
        return fixture_rows

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
    embedder.qdrant.retrieve = AsyncMock(
        return_value=[
            _vector_record_for_row(
                _persisted_chunk(
                    200,
                    1,
                    model=EMBEDDING_MODEL_NAME,
                    embedding_id=chunk_point_id(200, 1),
                    content="Also stable",
                )
            )
        ]
    )

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

    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    prior_rows = [
        _persisted_chunk(
            201,
            0,
            model=EMBEDDING_MODEL_NAME,
            embedding_id=chunk_point_id(201, 0),
            content="Orphaned content",
        )
    ]

    async def _fetch(_sql, *_params):
        return prior_rows

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


@pytest.mark.asyncio
async def test_run_process_pdf_resume_reembeds_when_vector_payload_is_stale():
    """A matching point ID cannot resume a chunk whose durable content identity is stale."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME

    conn = AsyncMock()
    conn.fetchval.side_effect = [0, None]
    conn.fetch = AsyncMock(
        return_value=[
            _persisted_chunk(
                202,
                0,
                model=EMBEDDING_MODEL_NAME,
                embedding_id=chunk_point_id(202, 0),
                content="current content",
            )
        ]
    )
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    chunk = SimpleNamespace(
        chunk_index=0,
        content="current content",
        page_number=1,
        start_char=0,
        end_char=15,
    )
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", [chunk], ["vec-a"]))
    embedder = MagicMock()
    embedder.qdrant.retrieve = AsyncMock(
        return_value=[
            SimpleNamespace(
                id=chunk_point_id(202, 0),
                payload={
                    "paper_id": 202,
                    "chunk_index": 0,
                    "embedding_model": EMBEDDING_MODEL_NAME,
                    "embedding_fingerprint": "stale",
                },
            )
        ]
    )

    await run_process_pdf(
        paper_id=202,
        pdf_path=Path("/tmp/paper.pdf"),
        db_pool=pool,
        pdf_processor=pdf_processor,
        embedder=embedder,
        force=False,
    )

    assert pdf_processor.process.await_args.kwargs["resume_content"] == {}


def _paper_for_upsert(source_type: str = "arxiv"):
    """Return a minimal valid paper model for upsert trust-boundary tests."""
    from paper_ingestion.models.papers import PaperCreate, SourceType

    return PaperCreate(
        external_id=f"upsert-{source_type}",
        source_type=SourceType(source_type),
        title="Upsert visibility",
        authors=["A. Author"],
        url="https://example.test/upsert",
    )


@pytest.mark.asyncio
async def test_verified_public_upsert_rejects_private_source_before_database() -> None:
    """A private-source model cannot reach the public write even through misuse."""
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    conn = AsyncMock()
    with pytest.raises(ValueError, match="not eligible for public visibility"):
        await upsert_verified_public_paper(conn, _paper_for_upsert("local"))

    conn.fetchrow.assert_not_awaited()
