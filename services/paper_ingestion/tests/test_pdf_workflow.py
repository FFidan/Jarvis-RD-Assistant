"""Unit tests for shared PDF workflow helpers."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
import torch

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
from jarvis_common.testing import SharedConnPool, make_pool_and_conn
from paper_ingestion.ingestion.payload_schema import VectorVisibility
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.services import embedding_reconcile as embedding_reconcile_module
from paper_ingestion.services import paper_content_reclaim as paper_content_reclaim_module
from paper_ingestion.services import paper_locks as paper_locks_module
from paper_ingestion.services import pdf_workflow as pdf_workflow_module
from paper_ingestion.services.pdf_workflow import (
    PDFRebuildNotPermittedError,
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
    # The generation resolver is read through two module namespaces: the PDF run
    # (pdf_workflow) and reconciliation (embedding_reconcile). Patch both so
    # either entry point sees the stubbed generation.
    resolve_generation = AsyncMock(return_value=_TEST_VISIBILITY_GENERATION)
    monkeypatch.setattr(
        pdf_workflow_module,
        "_resolve_visibility_generation",
        resolve_generation,
    )
    monkeypatch.setattr(
        embedding_reconcile_module,
        "_resolve_visibility_generation",
        resolve_generation,
    )
    monkeypatch.setattr(
        pdf_workflow_module,
        "_load_paper_embedding_context",
        AsyncMock(return_value=(_TEST_VECTOR_VISIBILITY, 17)),
    )


_MOCKED_SOURCE_URL = "https://example.test/mocked-source.pdf"

# A force run must name a requester who holds the paper. These mocked connections
# answer the membership probe from the same non-empty ``fetch`` stub they already
# use for the chunk reads, so the holder path is the one exercised.
_HOLDER_USER_ID = 11


def _fetchval_answers(*probes: object):
    """Return a ``fetchval`` side effect for a paper whose source URL never moves.

    ``run_process_pdf`` reads ``papers.pdf_url`` and then the download premise
    before processing, and re-reads the URL in the commit transaction; *probes*
    answer the chunk-count and ``chunked_at`` reads that sit between.
    """
    answers = iter((_MOCKED_SOURCE_URL, True, *probes))
    return lambda *_args, **_kwargs: next(answers, _MOCKED_SOURCE_URL)


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
    # Without a timeout the connection's session settings are left alone: the
    # reconciliation callers share pooled connections and wait deliberately.
    assert len(conn.execute.await_args_list) == 2


@pytest.mark.asyncio
async def test_advisory_lock_resets_its_timeout_after_the_wait_expires():
    """A lock that never arrives still leaves the pooled session as it found it.

    The reset has to survive the acquisition itself failing — that is the whole
    reason it lives outside the try that guards the lock body.
    """
    import asyncpg

    conn = AsyncMock()

    async def _execute(statement, *_args):
        if "pg_advisory_lock" in statement:
            raise asyncpg.exceptions.LockNotAvailableError("canceled on lock timeout")

    conn.execute = AsyncMock(side_effect=_execute)

    with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
        async with advisory_lock(conn, 2, 7, timeout_s=600):
            pytest.fail("the lock body must not run when the lock was never taken")

    statements = [call_args.args[0] for call_args in conn.execute.await_args_list]
    assert statements == [
        "SET lock_timeout = '600s'",
        "SELECT pg_advisory_lock($1, $2)",
        "SET lock_timeout = DEFAULT",
    ]


class _SingleSlotProbePool:
    """One-slot pool fake that models lock ownership and pool occupancy."""

    def __init__(self, *, lock_available: bool) -> None:
        self.lock_available = lock_available
        self.slot = asyncio.Semaphore(1)
        self.probe_released = asyncio.Event()
        self.in_use = 0
        self.probe_attempts = 0
        self.release_count = 0
        self.lock_held = False

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
                pool.probe_attempts += 1
                pool.lock_held = pool.lock_available
                return {"acquired": pool.lock_available}

            async def execute(self, _statement, *_args):
                pool.lock_held = False
                pool.release_count += 1

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
    assert pool.release_count == 0
    assert not pool.lock_held


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

    assert pool.probe_attempts == 1
    assert pool.release_count == 1
    assert not pool.lock_held
    assert pool.in_use == 0
    async with asyncio.timeout(0.1):
        async with pool.acquire():
            assert pool.in_use == 1


@pytest.mark.asyncio
async def test_paper_lock_probe_loop_gives_up_after_its_total_deadline(monkeypatch):
    """A permanently held paper lock refuses the caller instead of probing forever.

    The clock is faked so the real ten-minute deadline is exercised rather than a
    shortened stand-in, and the outer timeout turns "waits forever" into a
    failure instead of a hung suite.
    """
    pool = _SingleSlotProbePool(lock_available=False)
    real_sleep = asyncio.sleep
    slept: list[float] = []

    async def _fake_sleep(delay, *args, **kwargs):
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    with pytest.raises(pdf_workflow_module.PDFUserFacingError) as raised:
        async with asyncio.timeout(30):
            async with pdf_workflow_module._paper_mutation_connection(  # type: ignore[attr-defined]
                pool,
                77,  # type: ignore[arg-type]
            ):
                pytest.fail("the contended lock must never be reported as acquired")

    assert "Paper 77 is locked by another long-running operation" in str(raised.value)
    assert sum(slept) >= paper_locks_module._PAPER_LOCK_MAX_WAIT_SECONDS
    # ... and not one probe earlier: the refusal waits out the whole budget.
    assert sum(slept[:-1]) < paper_locks_module._PAPER_LOCK_MAX_WAIT_SECONDS
    assert pool.release_count == 0


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
    conn.fetchval.side_effect = _fetchval_answers(2, None)
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
        requester_id=_HOLDER_USER_ID,
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


# ---------------------------------------------------------------------------
# Holdership is required before a run may discard derived content
# ---------------------------------------------------------------------------


def _rebuildable_conn(*, library_rows: list) -> AsyncMock:
    """Return a connection on which a force run would complete if it were permitted.

    The paper has two persisted chunks and no ``chunked_at``, so a permitted run
    takes the discard-and-rebuild branch. ``library_rows`` answers the holdership
    probe, and the same ``fetch`` stub then answers the stale-vector read — so a
    non-empty value is shaped for both, keeping the run completable once the
    holdership refusal is out of the way.
    """
    conn = AsyncMock()
    conn.fetchval.side_effect = _fetchval_answers(2, None)
    conn.fetch.return_value = library_rows
    return conn


def _rebuildable_processor() -> MagicMock:
    """Return a PDF processor that would produce one fresh chunk for a permitted run."""
    chunks = [SimpleNamespace(chunk_index=0, content="A", page_number=1, start_char=0, end_char=1)]
    processor = MagicMock()
    processor.process = AsyncMock(return_value=("full text", chunks, ["vec-0"]))
    return processor


def _rebuildable_embedder() -> MagicMock:
    """Return an embedder whose stale-vector cleanup a permitted run can await."""
    embedder = MagicMock()
    embedder.qdrant.delete = AsyncMock()
    return embedder


@pytest.mark.asyncio
async def test_force_run_refuses_requester_who_does_not_hold_the_paper():
    """Being able to see a public paper does not permit discarding its content."""
    conn = _rebuildable_conn(library_rows=[])
    pool, _ = make_pool_and_conn(conn=conn)
    pdf_processor = _rebuildable_processor()

    with pytest.raises(PDFRebuildNotPermittedError, match="library"):
        await run_process_pdf(
            paper_id=5,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=_rebuildable_embedder(),
            force=True,
            requester_id=_HOLDER_USER_ID,
        )

    pdf_processor.process.assert_not_awaited()
    discard_chunks = call("DELETE FROM paper_chunks WHERE paper_id = $1", 5)
    assert discard_chunks not in conn.execute.await_args_list


@pytest.mark.asyncio
async def test_force_run_refuses_when_no_requester_is_named():
    """Fail-closed: an unnamed requester is refused even where a holder exists.

    The library probe would answer ``True`` here, so only the missing requester
    can produce the refusal — a caller that cannot name one is not let through.
    """
    conn = _rebuildable_conn(library_rows=[{"embedding_id": "vec-old"}])
    pool, _ = make_pool_and_conn(conn=conn)
    pdf_processor = _rebuildable_processor()

    with pytest.raises(PDFRebuildNotPermittedError, match="library"):
        await run_process_pdf(
            paper_id=5,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=_rebuildable_embedder(),
            force=True,
        )

    pdf_processor.process.assert_not_awaited()
    discard_chunks = call("DELETE FROM paper_chunks WHERE paper_id = $1", 5)
    assert discard_chunks not in conn.execute.await_args_list


@pytest.mark.asyncio
async def test_run_process_pdf_wraps_embedding_failures():
    """Embedding errors keep sanitized cause detail for operators."""
    conn = AsyncMock()
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)  # no existing chunks
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
# Purpose-built failure messages survive to the caller
# ---------------------------------------------------------------------------


def _embedding_batch_error(**kwargs):
    from paper_ingestion.ingestion.embedder import EmbeddingBatchError

    return EmbeddingBatchError("batch 1/5 failed immediately", **kwargs)


# Every failure whose message was written for the requester rather than copied
# from the upstream exception. Parameterised so a sixth such exit added later
# without the marker type shows up here as a failure rather than as a message
# that silently collapses to "Job failed" at the job boundary.
_USER_FACING_PROCESS_FAILURES = [
    pytest.param(
        _embedding_batch_error(completed_chunks=[], completed_point_ids=[]),
        "chunks saved",
        id="partial-embedding-batch",
    ),
    pytest.param(
        torch.OutOfMemoryError("simulated OOM"),
        "GPU out-of-memory",
        id="torch-oom",
    ),
    pytest.param(
        RuntimeError("CUDA out of memory: tried to allocate 2 GiB"),
        "GPU error",
        id="cuda-runtime-error",
    ),
    pytest.param(
        RuntimeError("embedding backend closed the connection"),
        "Embedding service error",
        id="generic-embedding-failure",
    ),
    pytest.param(
        httpx.HTTPStatusError(
            "503",
            request=httpx.Request("POST", "http://litellm/embed"),
            response=httpx.Response(503),
        ),
        "Embedding service error",
        id="embedding-http-status",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "expected_text"), _USER_FACING_PROCESS_FAILURES)
async def test_process_failures_carry_their_remediation_text(failure, expected_text):
    """Each purpose-built failure leaves the workflow as the marker type, text intact."""
    from paper_ingestion.services.pdf_workflow import PDFUserFacingError

    conn = AsyncMock()
    conn.fetchval.side_effect = _fetchval_answers(0)
    pool, _ = make_pool_and_conn(conn=conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=failure)

    with pytest.raises(PDFUserFacingError) as raised:
        await run_process_pdf(
            paper_id=91,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=MagicMock(),
        )

    assert expected_text in str(raised.value)


def _docling_conversion_error(message: str = "cannot decode") -> Exception:
    from docling.exceptions import ConversionError

    return ConversionError(message)


def _pdfium_decode_error(message: str = "cannot load document") -> Exception:
    from pypdfium2 import PdfiumError

    return PdfiumError(message)


# Docling's ConversionError and pypdfium2's PdfiumError both subclass
# RuntimeError, so before this fix they fell into the generic embedding-failure
# handler along with real embedding errors.
_DOCUMENT_READ_FAILURES = [
    pytest.param(_docling_conversion_error(), id="docling-conversion-error"),
    pytest.param(_pdfium_decode_error(), id="pdfium-decode-error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", _DOCUMENT_READ_FAILURES)
async def test_document_read_failure_is_not_reported_as_an_embedding_problem(failure):
    """A decode failure must name the document stage, not the model services.

    Docling raises ConversionError and pypdfium2 raises PdfiumError for an
    unreadable PDF; both subclass RuntimeError, so a broad catch previously
    reported them as embedding failures and sent operators to inspect healthy
    LLM services instead of the file itself.
    """
    from paper_ingestion.services.pdf_workflow import PDFUserFacingError

    conn = AsyncMock()
    conn.fetchval.side_effect = _fetchval_answers(0)
    pool, _ = make_pool_and_conn(conn=conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=failure)

    with pytest.raises(PDFUserFacingError) as raised:
        await run_process_pdf(
            paper_id=92,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=MagicMock(),
        )

    message = str(raised.value).lower()
    assert "embedding" not in message
    assert "litellm" not in message
    assert "ollama" not in message
    assert "read" in message or "convert" in message


def _process_route_app(tmp_path, monkeypatch, *, process_side_effect=None, lock_available=True):
    """Wire the synchronous process route over the unmocked workflow.

    The route's handler is what decides between the sanitized 502 and an
    unhandled 500, so failures have to be driven through it rather than asserted
    on a hand-called function.
    """
    from fastapi import FastAPI
    from jarvis_common import current_user_id_strict
    from paper_ingestion.deps import get_db_pool, get_embedder, get_pdf_processor
    from paper_ingestion.routers import pdf_actions as pdf_router
    from tests.conftest import FakeRecord

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    paper_path = storage_dir / "1.pdf"
    paper_path.write_bytes(b"%PDF-1.7\ncontent")

    conn = AsyncMock()
    conn.fetchval.side_effect = _fetchval_answers(0)
    paper_row = FakeRecord(
        id=1,
        pdf_downloaded=True,
        pdf_local_path=str(paper_path),
        is_visible=True,
    )

    async def _fetchrow(sql, *args):
        if "pg_try_advisory_lock" in sql:
            return {"acquired": lock_available}
        return paper_row

    conn.fetchrow.side_effect = _fetchrow
    pool, _ = make_pool_and_conn(conn=conn)

    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=process_side_effect)
    monkeypatch.setattr(pdf_router, "PDF_STORAGE_PATH", str(storage_dir))

    app = FastAPI()
    app.include_router(pdf_router.router)
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_pdf_processor] = lambda: pdf_processor
    app.dependency_overrides[get_embedder] = lambda: MagicMock()
    app.dependency_overrides[current_user_id_strict] = lambda: 1
    return app


async def _post_synchronous_process(app):
    """Drive one synchronous process request against *app*."""
    from unittest.mock import patch as mock_patch

    from jarvis_common.settings import CoreSettings

    with mock_patch(
        "paper_ingestion.routers.pdf_actions.get_core_settings",
        return_value=CoreSettings(dev_mode=True),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/api/process-pdf/1", params={"sync": True})


@pytest.mark.asyncio
async def test_synchronous_process_route_still_answers_502_for_an_embedding_failure(
    tmp_path, monkeypatch
):
    """The route sees a real workflow failure as a service error, not a crash."""
    app = _process_route_app(
        tmp_path,
        monkeypatch,
        process_side_effect=RuntimeError("embedding backend closed the connection"),
    )

    response = await _post_synchronous_process(app)

    # Through the real route, so an error type the handler does not catch shows up
    # as a 500 here rather than as a passing assertion on a hand-called function.
    assert response.status_code == 502
    assert "Embedding service error" in response.json()["detail"]["detail"]


@pytest.mark.asyncio
async def test_synchronous_process_route_answers_502_when_the_paper_lock_never_frees(
    tmp_path, monkeypatch
):
    """Giving up on a contended lock is a service error too, not a crash.

    A refusal raised as anything the route does not catch — a job-layer error,
    for one — turns this sanitized 502 into an unhandled 500.
    """
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    app = _process_route_app(tmp_path, monkeypatch, lock_available=False)

    async with asyncio.timeout(30):
        response = await _post_synchronous_process(app)

    assert response.status_code == 502
    assert "locked by another long-running operation" in response.json()["detail"]["detail"]


# ---------------------------------------------------------------------------
# torch OOM / CUDA error differentiation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_workflow_relabels_torch_oom_as_distinct_error():
    """torch.OutOfMemoryError is re-raised with a GPU-specific actionable message."""
    conn = AsyncMock()
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
        conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
        # No existing chunks (force or post-failure retry).
        conn.fetchval.side_effect = _fetchval_answers(0)
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
    # existing_count > 0 with chunked_at unset, plus force, triggers the force path.
    conn.fetchval.side_effect = _fetchval_answers(3, None)
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
        requester_id=_HOLDER_USER_ID,
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
    conn.fetchval.side_effect = _fetchval_answers(2, None)
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
        requester_id=_HOLDER_USER_ID,
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
    # 3 partial chunks, never marked complete.
    conn.fetchval.side_effect = _fetchval_answers(3, None)
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
    conn.fetchval.side_effect = _fetchval_answers(4, datetime(2026, 6, 17, tzinfo=UTC))
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
        embedding_reconcile_module,
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
        embedding_reconcile_module,
        "visibility_lease_is_current",
        _replace_then_confirm_lease,
    )
    await embedding_reconcile_module._delete_reconcile_generation(
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
            if "pdf_url" in sql:
                return _MOCKED_SOURCE_URL
            if "pdf_downloaded" in sql:
                return True
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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
    conn.fetchval.side_effect = _fetchval_answers(0)
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


# ---------------------------------------------------------------------------
# Live-PG contract: a promotion that replaces a paper's source URL must not be
# undone by a download or a processing run that began against the previous one.
# Both writes compare against state the enclosing transaction already owns.
# ---------------------------------------------------------------------------

_SUPERSEDED_PDF_URL = "https://example.test/superseded.pdf"
_TRUSTED_PDF_URL = "https://arxiv.org/pdf/2401.00099.pdf"
# The next revision the same version-stripped arXiv identifier resolves to.
_REFRESHED_PDF_URL = "https://arxiv.org/pdf/2401.00099v2.pdf"


def _paper_with_pdf_url(external_id: str, pdf_url: str):
    """Return arXiv metadata carrying an explicit source URL."""
    from paper_ingestion.models.papers import PaperCreate, SourceType

    return PaperCreate(
        external_id=external_id,
        source_type=SourceType.ARXIV,
        title="Source URL agreement",
        authors=["A. Author"],
        url="https://example.test/source-url-agreement",
        pdf_url=pdf_url,
    )


def _staged_download(staged: Path, final: Path):
    """Return a PDF processor double whose download has already been staged."""
    processor = MagicMock()
    processor.stage_pdf_download = AsyncMock(return_value=(staged, final))
    return processor


def _extraction(label: str):
    """Return the ``(text, chunks, point_ids)`` triple a PDF extraction yields."""
    chunk = SimpleNamespace(
        chunk_index=0,
        content=f"{label} content",
        page_number=1,
        start_char=0,
        end_char=len(label) + 8,
    )
    return (f"{label} text", [chunk], [f"vec-{label.lower()}"])


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_processing_refuses_a_file_an_earlier_promotion_left_behind(
    contract_conn, tmp_path: Path
) -> None:
    """A run starting after a promotion cannot process the file it left on disk.

    The promotion clears ``pdf_downloaded`` and ``pdf_local_path`` but unlinks
    nothing, so a caller that read the row before the promotion still holds a
    path to content derived from the previous source URL. Comparing the source
    URL alone would not catch this: such a run captures the post-promotion URL
    and would still agree with it at commit time.
    """
    # Verified: services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:1191
    from paper_ingestion.services.pdf_workflow import (
        PDFSourceSupersededError,
        upsert_paper,
        upsert_verified_public_paper,
    )

    seeded = await upsert_paper(
        contract_conn, _paper_with_pdf_url("premise-entry", _SUPERSEDED_PDF_URL)
    )
    paper_id = int(seeded["id"])
    pdf_path = tmp_path / f"{paper_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nsuperseded")
    await contract_conn.execute(
        "UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $2 WHERE id = $1",
        paper_id,
        str(pdf_path),
    )

    await upsert_verified_public_paper(
        contract_conn, _paper_with_pdf_url("premise-entry", _TRUSTED_PDF_URL)
    )
    assert pdf_path.exists(), "the promotion is not expected to remove the file"

    processor = MagicMock()
    processor.process = AsyncMock(return_value=_extraction("Superseded"))

    with pytest.raises(PDFSourceSupersededError):
        await run_process_pdf(
            paper_id, pdf_path, SharedConnPool(contract_conn), processor, MagicMock()
        )

    processor.process.assert_not_awaited()
    promoted = await contract_conn.fetchrow(
        """SELECT p.visibility_scope, p.chunked_at,
                  (SELECT COUNT(*) FROM paper_chunks c WHERE c.paper_id = p.id) AS chunk_count
             FROM papers p
            WHERE p.id = $1""",
        paper_id,
    )
    assert promoted["visibility_scope"] == "public"
    assert promoted["chunk_count"] == 0, "content from the left-behind file reached the public row"
    assert promoted["chunked_at"] is None


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_download_pointer_rejected_once_the_source_url_moved_on(
    contract_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download that began against a replaced source URL publishes nothing."""
    # Verified: services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:167
    from paper_ingestion.services.pdf_workflow import PDFRecordMissingError, upsert_paper

    seeded = await upsert_paper(
        contract_conn, _paper_with_pdf_url("fence-download", _SUPERSEDED_PDF_URL)
    )
    paper_id = int(seeded["id"])
    final = tmp_path / f"{paper_id}.pdf"
    final.write_bytes(b"%PDF-1.7\ntrusted")
    staged = tmp_path / f"_download_{paper_id}.pdf"
    staged.write_bytes(b"%PDF-1.7\nsuperseded")
    monkeypatch.setattr("paper_ingestion.pdf_processor.maintenance_active", lambda: False)

    # The row moves to the verified adapter's URL while this download is in flight.
    await contract_conn.execute(
        "UPDATE papers SET pdf_url = $2 WHERE id = $1", paper_id, _TRUSTED_PDF_URL
    )

    with pytest.raises(PDFRecordMissingError):
        await download_and_store_pdf(
            SharedConnPool(contract_conn),
            _staged_download(staged, final),
            _SUPERSEDED_PDF_URL,
            paper_id,
        )

    row = await contract_conn.fetchrow(
        "SELECT pdf_downloaded, pdf_local_path FROM papers WHERE id = $1", paper_id
    )
    assert row["pdf_downloaded"] is False, "the download flag was published for a replaced URL"
    assert row["pdf_local_path"] is None
    assert final.read_bytes() == b"%PDF-1.7\ntrusted", "the publication was not rolled back"
    assert not staged.exists()


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_chunk_commit_rejected_when_a_redownload_restores_the_same_local_path(
    contract_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored local path does not make in-flight content current again.

    The promotion clears ``pdf_downloaded`` and ``pdf_local_path``, which is
    exactly the state the auto-fetch download selection looks for. That download
    republishes the same deterministic ``{paper_id}.pdf`` slot, so the local path
    the in-flight run holds is restored byte for byte before it commits.
    """
    # Verified: services/paper_ingestion/paper_ingestion/pipelines/auto_fetch.py:171
    from paper_ingestion.services.pdf_workflow import (
        PDFSourceSupersededError,
        upsert_paper,
        upsert_verified_public_paper,
    )

    seeded = await upsert_paper(
        contract_conn, _paper_with_pdf_url("fence-restored-path", _SUPERSEDED_PDF_URL)
    )
    paper_id = int(seeded["id"])
    pdf_path = tmp_path / f"{paper_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nsuperseded")
    await contract_conn.execute(
        "UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $2 WHERE id = $1",
        paper_id,
        str(pdf_path),
    )
    monkeypatch.setattr("paper_ingestion.pdf_processor.maintenance_active", lambda: False)
    pool = SharedConnPool(contract_conn)
    staged = tmp_path / f"_download_{paper_id}.pdf"
    staged.write_bytes(b"%PDF-1.7\ntrusted")

    async def _promote_then_redownload(*_args, **_kwargs):
        """Promote, then let the pending-download sweep restore the same slot."""
        await upsert_verified_public_paper(
            contract_conn, _paper_with_pdf_url("fence-restored-path", _TRUSTED_PDF_URL)
        )
        restored = await download_and_store_pdf(
            pool, _staged_download(staged, pdf_path), _TRUSTED_PDF_URL, paper_id
        )
        assert restored["pdf_local_path"] == str(pdf_path), "the slot was not restored"
        return _extraction("Superseded")

    processor = MagicMock()
    processor.process = AsyncMock(side_effect=_promote_then_redownload)

    with pytest.raises(PDFSourceSupersededError):
        await run_process_pdf(paper_id, pdf_path, pool, processor, MagicMock())

    promoted = await contract_conn.fetchrow(
        """SELECT p.visibility_scope, p.chunked_at,
                  (SELECT COUNT(*) FROM paper_chunks c WHERE c.paper_id = p.id) AS chunk_count
             FROM papers p
            WHERE p.id = $1""",
        paper_id,
    )
    assert promoted["visibility_scope"] == "public"
    assert promoted["chunk_count"] == 0, "content from the replaced source reached the public row"
    assert promoted["chunked_at"] is None, "the public row was marked processed"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_chunk_commit_rejected_after_promotion_and_one_retry_converges(
    contract_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunks from a replaced file are refused, and a single retry stores the trusted ones."""
    # Verified: services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:1179
    from paper_ingestion.services.pdf_workflow import (
        PDFSourceSupersededError,
        upsert_paper,
        upsert_verified_public_paper,
    )

    seeded = await upsert_paper(
        contract_conn, _paper_with_pdf_url("fence-commit", _SUPERSEDED_PDF_URL)
    )
    paper_id = int(seeded["id"])
    pdf_path = tmp_path / f"{paper_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nsuperseded")
    await contract_conn.execute(
        "UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $2 WHERE id = $1",
        paper_id,
        str(pdf_path),
    )
    monkeypatch.setattr("paper_ingestion.pdf_processor.maintenance_active", lambda: False)
    pool = SharedConnPool(contract_conn)
    embedder = MagicMock()
    processor = MagicMock()

    async def _promote_between_processing_and_commit(*_args, **_kwargs):
        """Land the promotion in the window this run leaves between the two phases."""
        await upsert_verified_public_paper(
            contract_conn, _paper_with_pdf_url("fence-commit", _TRUSTED_PDF_URL)
        )
        return _extraction("Superseded")

    processor.process = AsyncMock(side_effect=_promote_between_processing_and_commit)

    with pytest.raises(PDFSourceSupersededError):
        await run_process_pdf(paper_id, pdf_path, pool, processor, embedder)

    promoted = await contract_conn.fetchrow(
        """SELECT p.visibility_scope, p.pdf_local_path, p.chunked_at,
                  (SELECT COUNT(*) FROM paper_chunks c WHERE c.paper_id = p.id) AS chunk_count
             FROM papers p
            WHERE p.id = $1""",
        paper_id,
    )
    assert promoted["visibility_scope"] == "public"
    assert promoted["chunk_count"] == 0, "content from the replaced file reached the public row"
    assert promoted["chunked_at"] is None, "the public row was marked processed"
    assert promoted["pdf_local_path"] is None

    # Retry: re-download from the URL the row now carries, then process once more.
    staged = tmp_path / f"_download_{paper_id}.pdf"
    staged.write_bytes(b"%PDF-1.7\ntrusted")
    republished = await download_and_store_pdf(
        pool, _staged_download(staged, pdf_path), _TRUSTED_PDF_URL, paper_id
    )
    assert republished["pdf_downloaded"] is True
    processor.process = AsyncMock(return_value=_extraction("Trusted"))

    result = await run_process_pdf(paper_id, pdf_path, pool, processor, embedder)

    assert result["status"] == "processed"
    stored = await contract_conn.fetch(
        "SELECT content FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index", paper_id
    )
    assert [record["content"] for record in stored] == ["Trusted content"]


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_resumable_chunks_discarded_when_promotion_precedes_the_partial_save(
    contract_conn, tmp_path: Path
) -> None:
    """A partially embedded run cannot leave its chunks on a paper promoted meanwhile."""
    # Verified: services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:1264
    from paper_ingestion.ingestion.embedder import EmbeddingBatchError
    from paper_ingestion.services.pdf_workflow import (
        upsert_paper,
        upsert_verified_public_paper,
    )

    seeded = await upsert_paper(
        contract_conn, _paper_with_pdf_url("fence-resume", _SUPERSEDED_PDF_URL)
    )
    paper_id = int(seeded["id"])
    pdf_path = tmp_path / f"{paper_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nsuperseded")
    await contract_conn.execute(
        "UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $2 WHERE id = $1",
        paper_id,
        str(pdf_path),
    )

    async def _promote_then_fail_midway(*_args, **_kwargs):
        """Land the promotion, then fail with the batches that already embedded."""
        await upsert_verified_public_paper(
            contract_conn, _paper_with_pdf_url("fence-resume", _TRUSTED_PDF_URL)
        )
        raise EmbeddingBatchError(
            "batch 2/4 failed: connection reset",
            completed_chunks=[
                ChunkForEmbedding(
                    chunk_index=0,
                    content="Superseded content",
                    page_number=1,
                    start_char=0,
                    end_char=18,
                )
            ],
            completed_point_ids=["vec-superseded"],
        )

    processor = MagicMock()
    processor.process = AsyncMock(side_effect=_promote_then_fail_midway)

    with pytest.raises(RuntimeError, match="source changed while it was being processed") as raised:
        await run_process_pdf(
            paper_id, pdf_path, SharedConnPool(contract_conn), processor, MagicMock()
        )

    message = str(raised.value)
    assert "chunks saved" not in message, "the message must not claim a discarded save"
    assert "Process the paper again" in message, "the message must name the remedy"
    assert "Embedding service" not in message, (
        "the source change, not the embedding service, is why this run kept nothing"
    )

    promoted = await contract_conn.fetchrow(
        """SELECT p.visibility_scope,
                  (SELECT COUNT(*) FROM paper_chunks c WHERE c.paper_id = p.id) AS chunk_count
             FROM papers p
            WHERE p.id = $1""",
        paper_id,
    )
    assert promoted["visibility_scope"] == "public"
    assert promoted["chunk_count"] == 0, "resumable content from the replaced file was saved"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_chunk_commit_rejected_when_an_already_public_row_changes_source(
    contract_conn, tmp_path: Path
) -> None:
    """A public row invalidates stored content and refuses an in-flight commit.

    The refresh resets content derived from the previous URL. Independently,
    the commit-time fence rejects the in-flight run because its download premise
    no longer matches the row. Both protections must agree that content from the
    superseded URL is unusable.
    """
    # Verified: services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:296
    from paper_ingestion.services.pdf_workflow import (
        PDFSourceSupersededError,
        upsert_verified_public_paper,
    )

    seeded = await upsert_verified_public_paper(
        contract_conn, _paper_with_pdf_url("fence-public-row", _TRUSTED_PDF_URL)
    )
    assert seeded["visibility_scope"] == "public", "precondition: the row starts out public"
    paper_id = int(seeded["id"])
    pdf_path = tmp_path / f"{paper_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\ntrusted")
    await contract_conn.execute(
        "UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $2 WHERE id = $1",
        paper_id,
        str(pdf_path),
    )

    async def _refresh_the_public_row(*_args, **_kwargs):
        """Move the public row on to the adapter's next revision URL mid-run."""
        await upsert_verified_public_paper(
            contract_conn, _paper_with_pdf_url("fence-public-row", _REFRESHED_PDF_URL)
        )
        return _extraction("Superseded")

    processor = MagicMock()
    processor.process = AsyncMock(side_effect=_refresh_the_public_row)

    with pytest.raises(PDFSourceSupersededError):
        await run_process_pdf(
            paper_id, pdf_path, SharedConnPool(contract_conn), processor, MagicMock()
        )

    refreshed = await contract_conn.fetchrow(
        """SELECT p.pdf_url, p.pdf_local_path, p.chunked_at,
                  (SELECT COUNT(*) FROM paper_chunks c WHERE c.paper_id = p.id) AS chunk_count
             FROM papers p
            WHERE p.id = $1""",
        paper_id,
    )
    assert refreshed["pdf_url"] == _REFRESHED_PDF_URL
    assert refreshed["chunk_count"] == 0, "content derived from the previous URL was committed"
    assert refreshed["chunked_at"] is None
    assert refreshed["pdf_local_path"] is None


class _PauseAfterFetchrow:
    """Delegate to a real connection, pausing once after a selected row read."""

    def __init__(self, conn, sql_fragment: str) -> None:
        self._conn = conn
        self._sql_fragment = sql_fragment
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    async def fetchrow(self, sql: str, *args):
        row = await self._conn.fetchrow(sql, *args)
        if not self.paused.is_set() and self._sql_fragment in sql:
            self.paused.set()
            await self.release.wait()
        return row

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


async def _wait_for_database_lock(conn, backend_pid: int) -> None:
    """Wait until one known backend is blocked on a PostgreSQL lock."""
    for _ in range(200):
        wait_event_type = await conn.fetchval(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1",
            backend_pid,
        )
        if wait_event_type == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"backend {backend_pid} never waited on the paper row lock")


@pytest.mark.contract
@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_source_refresh_and_highlight_creation_share_the_paper_row_lock(
    test_db_pool,
) -> None:
    """Concurrent refreshes and annotations keep one generation order."""
    from paper_ingestion.models import HighlightCreate, HighlightRect, Rect
    from paper_ingestion.routers.highlights import create_highlight
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    source_id = "generation-lock-source"
    source_v1 = _paper_with_pdf_url(source_id, _TRUSTED_PDF_URL)
    source_v2 = _paper_with_pdf_url(source_id, _REFRESHED_PDF_URL)
    async with test_db_pool.acquire() as setup_conn:
        source_row = await upsert_verified_public_paper(setup_conn, source_v1)

    async with test_db_pool.acquire() as stale_conn:
        async with test_db_pool.acquire() as fresh_conn:
            async with test_db_pool.acquire() as observer_conn:
                paused_stale_conn = _PauseAfterFetchrow(
                    stale_conn,
                    "SELECT pdf_url FROM papers",
                )
                stale_refresh = asyncio.create_task(
                    upsert_verified_public_paper(paused_stale_conn, source_v1)
                )
                await paused_stale_conn.paused.wait()
                fresh_refresh = asyncio.create_task(
                    upsert_verified_public_paper(fresh_conn, source_v2)
                )
                try:
                    await _wait_for_database_lock(
                        observer_conn,
                        fresh_conn.get_server_pid(),
                    )
                finally:
                    paused_stale_conn.release.set()
                    await asyncio.gather(
                        stale_refresh,
                        fresh_refresh,
                        return_exceptions=True,
                    )
                stale_refresh.result()
                fresh_refresh.result()

    async with test_db_pool.acquire() as check_conn:
        refreshed = await check_conn.fetchrow(
            "SELECT pdf_url, content_generation FROM papers WHERE id = $1",
            source_row["id"],
        )
    assert refreshed["pdf_url"] == _REFRESHED_PDF_URL
    assert refreshed["content_generation"] == 1

    highlight_id = "generation-lock-highlight"
    highlight_v1 = _paper_with_pdf_url(highlight_id, _TRUSTED_PDF_URL)
    highlight_v2 = _paper_with_pdf_url(highlight_id, _REFRESHED_PDF_URL)
    async with test_db_pool.acquire() as setup_conn:
        user_id = int(
            await setup_conn.fetchval(
                "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
                "generation-lock@example.test",
            )
        )
        highlight_paper = await upsert_verified_public_paper(setup_conn, highlight_v1)
        await setup_conn.execute(
            """UPDATE papers
                  SET pdf_downloaded = TRUE, pdf_local_path = '/tmp/generation-lock.pdf'
                WHERE id = $1""",
            highlight_paper["id"],
        )

    rect = HighlightRect(
        boundingRect=Rect(x0=0.1, y0=0.1, x1=0.2, y1=0.2),
        rects=[Rect(x0=0.1, y0=0.1, x1=0.2, y1=0.2)],
    )
    body = HighlightCreate(page=1, rect=rect, quote="Locked document")
    handler = getattr(create_highlight, "__wrapped__", create_highlight)

    async with test_db_pool.acquire() as highlight_conn:
        async with test_db_pool.acquire() as promotion_conn:
            async with test_db_pool.acquire() as observer_conn:
                paused_highlight_conn = _PauseAfterFetchrow(
                    highlight_conn,
                    "SELECT p.source_type FROM papers p",
                )
                highlight_task = asyncio.create_task(
                    handler(
                        MagicMock(),
                        int(highlight_paper["id"]),
                        body,
                        SharedConnPool(paused_highlight_conn),
                        user_id,
                    )
                )
                await paused_highlight_conn.paused.wait()
                promotion_task = asyncio.create_task(
                    upsert_verified_public_paper(promotion_conn, highlight_v2)
                )
                try:
                    await _wait_for_database_lock(
                        observer_conn,
                        promotion_conn.get_server_pid(),
                    )
                finally:
                    paused_highlight_conn.release.set()
                    await asyncio.gather(
                        highlight_task,
                        promotion_task,
                        return_exceptions=True,
                    )
                created = highlight_task.result()
                promotion_task.result()

    assert created.stale is False
    async with test_db_pool.acquire() as check_conn:
        stale = await check_conn.fetchval(
            """SELECT h.content_generation <> p.content_generation
                 FROM paper_highlights h
                 JOIN papers p ON p.id = h.paper_id
                WHERE h.id = $1""",
            created.id,
        )
    assert stale is True


# ---------------------------------------------------------------------------
# The promotion discard without a database: the delete it issues and the reset
# state the record it returns carries.
# ---------------------------------------------------------------------------


_PROMOTED_PAPER_ID = 4242
_PROMOTED_ROW = {
    "id": _PROMOTED_PAPER_ID,
    "external_id": "promotion-discard",
    "pdf_url": _TRUSTED_PDF_URL,
    "pdf_downloaded": True,
    "pdf_local_path": f"/data/pdfs/{_PROMOTED_PAPER_ID}.pdf",
    "chunked_at": datetime(2026, 1, 1, tzinfo=UTC),
    "is_insert": False,
}
_RESET_ROW = {
    **_PROMOTED_ROW,
    "pdf_downloaded": False,
    "pdf_local_path": None,
    "chunked_at": None,
}


def _promoting_conn(*reads: object) -> AsyncMock:
    """Return a connection mock answering the promotion's ``fetchrow`` reads in order.

    The reads are the pre-promotion state, the upserted row, and — only when the
    promotion discards — the row its derived-state reset returns.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(reads))
    # asyncpg reports the rows a statement removed in its command tag.
    conn.execute = AsyncMock(return_value="DELETE 3")
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    return conn


@pytest.mark.asyncio
async def test_promotion_discards_derived_chunks_when_it_replaces_the_source_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Refreshing a public row to a different source URL deletes its chunk rows."""
    from paper_ingestion.services.paper_upsert import _DELETE_DERIVED_CHUNKS_SQL
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    promoted = _PROMOTED_ROW
    conn = _promoting_conn(
        {"visibility_scope": "public", "pdf_url": _SUPERSEDED_PDF_URL},
        promoted,
        _RESET_ROW,
    )

    with caplog.at_level(logging.INFO, logger="paper_ingestion.services.paper_upsert"):
        record = await upsert_verified_public_paper(
            conn, _paper_with_pdf_url("promotion-discard", _TRUSTED_PDF_URL)
        )

    conn.execute.assert_awaited_once_with(_DELETE_DERIVED_CHUNKS_SQL, 4242)
    assert record["pdf_downloaded"] is False
    assert record["pdf_local_path"] is None
    assert record["chunked_at"] is None

    logged = [r for r in caplog.records if r.name == "paper_ingestion.services.paper_upsert"]
    assert len(logged) == 1, f"expected one discard record, got {[r.getMessage() for r in logged]}"
    assert logged[0].levelno == logging.INFO
    assert logged[0].args == (3, 4242, "promotion-discard"), (
        "the record must carry the rows removed, the paper id and its external id"
    )


@pytest.mark.asyncio
async def test_promotion_records_the_id_of_the_paper_it_discarded() -> None:
    """A discarding promotion appends its paper id to the caller's collector.

    The collector is the only thing that makes the storage a discard leaves
    behind reachable: callers drain it after their transaction commits, and an
    id that is never appended is storage nothing will ever free.
    """
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    conn = _promoting_conn(
        {"visibility_scope": "private", "pdf_url": _SUPERSEDED_PDF_URL},
        _PROMOTED_ROW,
        _RESET_ROW,
    )
    collected: list[int] = []

    await upsert_verified_public_paper(
        conn,
        _paper_with_pdf_url("promotion-discard", _TRUSTED_PDF_URL),
        discarded_content_ids=collected,
    )

    assert collected == [_PROMOTED_PAPER_ID], (
        f"the discarded paper's id must reach the caller's collector, got {collected}"
    )


@pytest.mark.asyncio
async def test_promotion_that_discards_nothing_records_no_id() -> None:
    """A promotion leaving the content in place hands the collector nothing.

    An unchanged source URL keeps its derived content, so the reclaim pass must
    never be handed an id whose files and vector points the paper still points
    at.
    """
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    conn = _promoting_conn(
        {"visibility_scope": "public", "pdf_url": _TRUSTED_PDF_URL},
        _PROMOTED_ROW,
    )
    collected: list[int] = []

    record = await upsert_verified_public_paper(
        conn,
        _paper_with_pdf_url("promotion-discard", _TRUSTED_PDF_URL),
        discarded_content_ids=collected,
    )

    assert collected == [], f"nothing was discarded, so no id may be recorded: {collected}"
    conn.execute.assert_not_awaited()
    assert record["pdf_local_path"] == _PROMOTED_ROW["pdf_local_path"]


# ---------------------------------------------------------------------------
# Reclaiming the storage a discard left behind: what it removes, the premise it
# re-reads before removing anything, and the failures it absorbs.
# ---------------------------------------------------------------------------

_RECLAIMED_PAPER_ID = 4242
_FRESHLY_DOWNLOADED_PDF = b"%PDF-1.4 freshly downloaded"
_FRESHLY_RENDERED_PAGE = b"freshly rendered"


def _stored_content_for_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Point both storage roots at *tmp_path* and fill them for the reclaimed paper.

    Returns the stored PDF path and the page-image directory a completed run
    would have left behind.
    """
    pdf_root = tmp_path / "pdfs"
    snapshot_root = tmp_path / "snapshots"
    pdf_root.mkdir()
    pdf_path = pdf_root / f"{_RECLAIMED_PAPER_ID}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 superseded")
    snapshot_dir = snapshot_root / str(_RECLAIMED_PAPER_ID)
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "page_1.png").write_bytes(b"")
    monkeypatch.setattr(paper_content_reclaim_module, "PDF_STORAGE_PATH", str(pdf_root))
    monkeypatch.setattr(paper_content_reclaim_module, "SNAPSHOT_STORAGE_PATH", str(snapshot_root))
    return pdf_path, snapshot_dir


def _record_reclaimed_vectors(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the vector delete with a recorder and return the list it fills."""
    deleted: list[int] = []

    async def _delete(paper_id: int) -> None:
        deleted.append(paper_id)

    monkeypatch.setattr(paper_content_reclaim_module, "delete_paper_vectors", _delete)
    return deleted


class _PaperMutationPool:
    """Pool fake whose lock probes model one PostgreSQL advisory-lock session."""

    def __init__(self, *, discarded: bool) -> None:
        self.discarded = discarded
        self.lock = asyncio.Lock()
        self.contended = asyncio.Event()
        self.lock_binds: list[tuple[str, tuple[object, ...]]] = []
        self.unlock_binds: list[tuple[str, tuple[object, ...]]] = []

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def fetchrow(self, statement: str, *args: object):
                pool.lock_binds.append((statement, args))
                if pool.lock.locked():
                    pool.contended.set()
                    return {"acquired": False}
                await pool.lock.acquire()
                return {"acquired": True}

            async def fetchval(self, _statement: str, *_args: object):
                return pool.discarded

            async def execute(self, statement: str, *args: object):
                pool.unlock_binds.append((statement, args))
                pool.lock.release()

        return _Acquire()


@pytest.mark.asyncio
async def test_reclamation_waits_for_a_publisher_then_spares_its_fresh_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publisher that holds the lock first makes reclamation observe its new state."""
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    vectors = ["superseded"]
    deleted: list[int] = []
    pool = _PaperMutationPool(discarded=True)
    publisher_started = asyncio.Event()
    publisher_may_finish = asyncio.Event()

    async def _delete(paper_id: int) -> None:
        deleted.append(paper_id)
        vectors.clear()

    async def _publisher() -> None:
        async with pdf_workflow_module._paper_mutation_connection(  # type: ignore[attr-defined]
            pool,
            _RECLAIMED_PAPER_ID,  # type: ignore[arg-type]
        ):
            publisher_started.set()
            await publisher_may_finish.wait()
            pool.discarded = False
            vectors[:] = ["fresh"]
            pdf_path.write_bytes(_FRESHLY_DOWNLOADED_PDF)
            (snapshot_dir / "page_1.png").write_bytes(_FRESHLY_RENDERED_PAGE)

    monkeypatch.setattr(paper_content_reclaim_module, "delete_paper_vectors", _delete)
    publisher = asyncio.create_task(_publisher())
    await publisher_started.wait()
    reclamation = asyncio.create_task(reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool))
    await asyncio.wait_for(pool.contended.wait(), timeout=1)
    publisher_may_finish.set()
    await asyncio.gather(publisher, reclamation)

    assert deleted == []
    assert vectors == ["fresh"]
    assert pdf_path.read_bytes() == _FRESHLY_DOWNLOADED_PDF
    assert (snapshot_dir / "page_1.png").read_bytes() == _FRESHLY_RENDERED_PAGE
    assert pool.lock_binds == [
        ("SELECT pg_try_advisory_lock($1, $2) AS acquired", (1, _RECLAIMED_PAPER_ID)),
        ("SELECT pg_try_advisory_lock($1, $2) AS acquired", (1, _RECLAIMED_PAPER_ID)),
        ("SELECT pg_try_advisory_lock($1, $2) AS acquired", (1, _RECLAIMED_PAPER_ID)),
    ]
    assert pool.unlock_binds == [
        ("SELECT pg_advisory_unlock($1, $2)", (1, _RECLAIMED_PAPER_ID)),
        ("SELECT pg_advisory_unlock($1, $2)", (1, _RECLAIMED_PAPER_ID)),
    ]


@pytest.mark.asyncio
async def test_reclamation_finishes_before_a_waiting_publisher_writes_fresh_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publisher waiting on reclamation writes the generation that survives it."""
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    vectors = ["superseded"]
    deleted_started = asyncio.Event()
    delete_may_finish = asyncio.Event()
    pool = _PaperMutationPool(discarded=True)

    async def _delete(_paper_id: int) -> None:
        deleted_started.set()
        await delete_may_finish.wait()
        vectors.clear()

    async def _publisher() -> None:
        async with pdf_workflow_module._paper_mutation_connection(  # type: ignore[attr-defined]
            pool,
            _RECLAIMED_PAPER_ID,  # type: ignore[arg-type]
        ):
            pool.discarded = False
            vectors[:] = ["fresh"]
            pdf_path.write_bytes(_FRESHLY_DOWNLOADED_PDF)
            snapshot_dir.mkdir()
            (snapshot_dir / "page_1.png").write_bytes(_FRESHLY_RENDERED_PAGE)

    monkeypatch.setattr(paper_content_reclaim_module, "delete_paper_vectors", _delete)
    reclamation = asyncio.create_task(reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool))
    await deleted_started.wait()
    publisher = asyncio.create_task(_publisher())
    await asyncio.wait_for(pool.contended.wait(), timeout=1)
    delete_may_finish.set()
    await asyncio.gather(reclamation, publisher)

    assert vectors == ["fresh"]
    assert pdf_path.read_bytes() == _FRESHLY_DOWNLOADED_PDF
    assert (snapshot_dir / "page_1.png").read_bytes() == _FRESHLY_RENDERED_PAGE


@pytest.mark.asyncio
async def test_reclamation_removes_the_vectors_the_stored_pdf_and_the_page_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paper still describing a discard gives up all three at once."""
    from paper_ingestion.services.paper_content_reclaim import _DISCARDED_CONTENT_STATE_SQL
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    pool, conn = make_pool_and_conn(fetchval_return=True)

    await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert (
        conn.fetchval.await_args_list
        == [call(_DISCARDED_CONTENT_STATE_SQL, _RECLAIMED_PAPER_ID)] * 2
    ), (
        "the premise gates the vector delete and is then re-read for the file steps, "
        f"got {conn.fetchval.await_args_list}"
    )
    conn.fetchrow.assert_awaited_once_with(
        "SELECT pg_try_advisory_lock($1, $2) AS acquired", 1, _RECLAIMED_PAPER_ID
    )
    conn.execute.assert_awaited_once_with(
        "SELECT pg_advisory_unlock($1, $2)", 1, _RECLAIMED_PAPER_ID
    )
    assert deleted_vector_ids == [_RECLAIMED_PAPER_ID]
    assert not pdf_path.exists(), "the PDF stored for the superseded source URL must be removed"
    assert not snapshot_dir.exists(), "page images must be removed whole, not page by page"


@pytest.mark.asyncio
async def test_reclamation_skips_a_paper_that_stores_content_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A paper re-downloaded inside the deferral window keeps every file and vector.

    The discard leaves exactly the state the download sweep selects on and the
    promotion has just written the source URL it fetches, so a fresh PDF, fresh
    page images and fresh vectors can already be in place by the time the
    deferred reclamation runs. The re-read of the paper's state is what stops
    those being deleted, and it reports this cause at INFO because it is the
    routine one.
    """
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    pool, _conn = make_pool_and_conn(fetchval_return=False)

    with caplog.at_level(logging.INFO, logger="paper_ingestion.services.paper_content_reclaim"):
        await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert deleted_vector_ids == [], "a paper storing content again must keep its vectors"
    assert pdf_path.exists(), "the freshly downloaded PDF must survive"
    assert snapshot_dir.exists(), "the freshly rendered page images must survive"
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert "stores derived content again" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_reclamation_reports_a_paper_that_no_longer_exists_as_content_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deleted paper row leaves its storage behind, and the record says so.

    ``IS NULL`` never evaluates to NULL, so the read answering NULL means the
    row itself is gone rather than its columns being unset. Nothing will ask for
    that paper's files or vector points again, so this is the one skip that
    strands storage for good and it must not be reported as the routine cause.
    """
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    pool, _conn = make_pool_and_conn(fetchval_return=None)

    with caplog.at_level(logging.INFO, logger="paper_ingestion.services.paper_content_reclaim"):
        await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert deleted_vector_ids == []
    assert pdf_path.exists()
    assert snapshot_dir.exists()
    assert [r.levelno for r in caplog.records] == [logging.WARNING], (
        f"a permanently stranded paper must not log as routine: {caplog.records}"
    )
    assert "left behind" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_reclamation_spares_content_stored_after_the_premise_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download committing inside the vector round trip keeps its file and page images.

    The state a discard leaves is exactly what the download sweep selects on,
    and the vector-store round trip sits between the premise read and the file
    steps. This models the download committing inside that gap: the state read
    before the round trip is already stale by the time the files would be
    removed, so a single read cannot decide the file steps.
    """
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    page_image = snapshot_dir / "page_1.png"
    pool, conn = make_pool_and_conn()
    conn.fetchval = AsyncMock(side_effect=[True, False])

    async def _download_commits_during_the_vector_delete(paper_id: int) -> None:
        pdf_path.write_bytes(_FRESHLY_DOWNLOADED_PDF)
        page_image.write_bytes(_FRESHLY_RENDERED_PAGE)

    monkeypatch.setattr(
        paper_content_reclaim_module,
        "delete_paper_vectors",
        _download_commits_during_the_vector_delete,
    )

    await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert pdf_path.exists() and pdf_path.read_bytes() == _FRESHLY_DOWNLOADED_PDF, (
        "the PDF the download stored inside the window must survive"
    )
    assert page_image.exists() and page_image.read_bytes() == _FRESHLY_RENDERED_PAGE, (
        "the page images rendered from it must survive with it"
    )


@pytest.mark.asyncio
async def test_reclamation_skips_when_the_state_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable premise leaves the content alone and never reaches the caller."""
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    pool, _conn = make_pool_and_conn(raise_on_acquire=RuntimeError("pool exhausted"))

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.paper_content_reclaim"):
        await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert deleted_vector_ids == []
    assert pdf_path.exists()
    assert snapshot_dir.exists()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


@pytest.mark.asyncio
async def test_reclamation_absorbs_a_vector_store_failure_and_still_frees_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A vector store that refuses the delete does not stop the file steps."""
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)

    async def _fail(paper_id: int) -> None:
        raise RuntimeError("vector store unavailable")

    monkeypatch.setattr(paper_content_reclaim_module, "delete_paper_vectors", _fail)
    pool, _conn = make_pool_and_conn(fetchval_return=True)

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.paper_content_reclaim"):
        await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert not pdf_path.exists()
    assert not snapshot_dir.exists()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


@pytest.mark.asyncio
async def test_reclamation_absorbs_an_unremovable_stored_pdf_and_still_frees_the_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stored PDF the filesystem refuses to unlink does not stop the image step."""
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    # A directory occupying the stored PDF's name: unlink refuses to remove it.
    pdf_path.unlink()
    pdf_path.mkdir()
    pool, _conn = make_pool_and_conn(fetchval_return=True)

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.paper_content_reclaim"):
        await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert deleted_vector_ids == [_RECLAIMED_PAPER_ID]
    assert pdf_path.exists(), "the failing step leaves what it could not remove"
    assert not snapshot_dir.exists(), "the step after the failing one still runs"
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


@pytest.mark.asyncio
async def test_reclamation_absorbs_an_unremovable_page_image_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A page-image directory the filesystem refuses to remove never reaches the caller."""
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    # A plain file occupying the image directory's name: rmtree refuses it.
    shutil.rmtree(snapshot_dir)
    snapshot_dir.write_bytes(b"not a directory")
    pool, _conn = make_pool_and_conn(fetchval_return=True)

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.paper_content_reclaim"):
        await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert deleted_vector_ids == [_RECLAIMED_PAPER_ID]
    assert not pdf_path.exists(), "the earlier step still ran"
    assert snapshot_dir.exists()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


@pytest.mark.asyncio
async def test_reclamation_ignores_a_paper_that_never_had_page_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A paper with no rendered pages logs nothing: the absent directory is expected."""
    from paper_ingestion.services.pdf_workflow import reclaim_discarded_paper_content

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    shutil.rmtree(snapshot_dir)
    pool, _conn = make_pool_and_conn(fetchval_return=True)

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.paper_content_reclaim"):
        await reclaim_discarded_paper_content(_RECLAIMED_PAPER_ID, pool)

    assert deleted_vector_ids == [_RECLAIMED_PAPER_ID]
    assert not pdf_path.exists()
    assert caplog.records == []


# ---------------------------------------------------------------------------
# A run voided by a promotion gives up what it wrote outside PostgreSQL.
# ---------------------------------------------------------------------------

_PROMOTED_PDF_URL = "https://arxiv.org/pdf/2401.00099.pdf"


def _promoted_mid_run_answers():
    """Return a ``fetchval`` side effect for a run promoted between start and commit.

    Answers are chosen by statement rather than by call order, so a read added
    to the workflow cannot silently shift every later answer by one.
    """
    from paper_ingestion.services.paper_content_reclaim import _DISCARDED_CONTENT_STATE_SQL
    from paper_ingestion.services.pdf_workflow import (
        _LOCKED_PAPER_SOURCE_URL_SQL,
        _PAPER_PDF_READY_SQL,
        _PAPER_SOURCE_URL_SQL,
    )

    def _answer(statement: str, *_args: object) -> object:
        if statement == _PAPER_SOURCE_URL_SQL:
            return _MOCKED_SOURCE_URL  # the URL this run starts against
        if statement == _PAPER_PDF_READY_SQL:
            return True  # the row still names the file it was handed
        if statement == _LOCKED_PAPER_SOURCE_URL_SQL:
            return _PROMOTED_PDF_URL  # the promotion landed while it worked
        if statement == _DISCARDED_CONTENT_STATE_SQL:
            return True  # and cleared the columns describing derived content
        return 0  # no chunk rows are stored yet

    return _answer


def _voided_run(pdf_path: Path, conn: AsyncMock):
    """Return the ``run_process_pdf`` keyword arguments for a promoted paper."""
    pool, _ = make_pool_and_conn(conn=conn)
    processor = MagicMock()
    processor.process = AsyncMock(return_value=_extraction("Voided"))
    return {
        "paper_id": _RECLAIMED_PAPER_ID,
        "pdf_path": pdf_path,
        "db_pool": pool,
        "pdf_processor": processor,
        "embedder": MagicMock(),
    }


@pytest.mark.asyncio
async def test_run_voided_by_a_promotion_gives_up_its_vectors_and_page_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion landing mid-run leaves behind neither page images nor vector points.

    The run derives content and writes its vectors before the commit fence
    rejects it, and the rollback returns only the SQL. Point ids are
    deterministic per paper and chunk index, so what it wrote outside
    PostgreSQL has to be given up while the per-paper lock still excludes any
    other run for this paper.
    """
    from paper_ingestion.services.pdf_workflow import PDFSourceSupersededError

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    conn = AsyncMock()
    conn.fetchval.side_effect = _promoted_mid_run_answers()

    with pytest.raises(PDFSourceSupersededError) as raised:
        await run_process_pdf(**_voided_run(pdf_path, conn))

    assert "no longer carries the source URL this run processed" in str(raised.value)
    assert deleted_vector_ids == [_RECLAIMED_PAPER_ID], (
        "the vector points the voided run wrote must be given up"
    )
    assert not snapshot_dir.exists(), "its page images must not stay servable under the paper"
    assert not pdf_path.exists()


@pytest.mark.asyncio
async def test_a_reclamation_failure_still_reports_the_promotion_to_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Storage the cleanup cannot free is recorded, never raised over the run's own error.

    The caller retries from the paper's current source URL on this error, so an
    incidental cleanup failure must not reach it wearing a different type.
    """
    from paper_ingestion.services.pdf_workflow import PDFSourceSupersededError

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    _record_reclaimed_vectors(monkeypatch)

    def _refuse_publication(_storage_path: Path):
        raise RuntimeError("publication lock unavailable")

    monkeypatch.setattr(paper_content_reclaim_module, "pdf_publish_operation", _refuse_publication)
    conn = AsyncMock()
    conn.fetchval.side_effect = _promoted_mid_run_answers()

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.paper_content_reclaim"):
        with pytest.raises(PDFSourceSupersededError):
            await run_process_pdf(**_voided_run(pdf_path, conn))

    assert snapshot_dir.exists(), "the step that failed is the one under test"
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


@pytest.mark.asyncio
async def test_run_voided_while_its_embedding_batch_fails_gives_up_what_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion landing during a failing embedding run reclaims that run's storage too.

    The batches that did embed wrote their vectors and page images, and the
    partial save meant to make them resumable is refused by the same fence. The
    run therefore keeps nothing in PostgreSQL, so what it wrote outside has to
    be given up as well — this exit is not a lesser one than the fully embedded
    run's.
    """
    from paper_ingestion.ingestion.embedder import EmbeddingBatchError

    pdf_path, snapshot_dir = _stored_content_for_reclaim(tmp_path, monkeypatch)
    deleted_vector_ids = _record_reclaimed_vectors(monkeypatch)
    conn = AsyncMock()
    conn.fetchval.side_effect = _promoted_mid_run_answers()
    voided = _voided_run(pdf_path, conn)
    voided["pdf_processor"].process = AsyncMock(
        side_effect=EmbeddingBatchError(
            "batch 2/4 failed: connection reset",
            completed_chunks=[
                ChunkForEmbedding(
                    chunk_index=0,
                    content="Voided content",
                    page_number=1,
                    start_char=0,
                    end_char=14,
                )
            ],
            completed_point_ids=["vec-voided"],
        )
    )

    from paper_ingestion.services.pdf_workflow import PDFSourceSupersededError  # noqa: PLC0415

    # The type, not just the message: the message alone reads the same whether or
    # not this exit reaches the handler that gives the storage back.
    with pytest.raises(
        PDFSourceSupersededError, match="source changed while it was being processed"
    ):
        await run_process_pdf(**voided)

    assert deleted_vector_ids == [_RECLAIMED_PAPER_ID], (
        "the vector points the completed batches wrote must be given up"
    )
    assert not snapshot_dir.exists(), "its page images must not stay servable under the paper"
    assert not pdf_path.exists()
