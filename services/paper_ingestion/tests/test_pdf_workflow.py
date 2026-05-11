"""Unit tests for shared PDF workflow helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import torch

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
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
