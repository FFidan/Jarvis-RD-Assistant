"""Unit tests for shared PDF workflow helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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
async def test_run_process_pdf_raises_when_qdrant_cleanup_fails():
    """Force-reprocessing fails clearly if old vectors cannot be removed."""
    conn = AsyncMock()
    conn.fetchval.return_value = 2
    conn.fetch.return_value = [{"embedding_id": "vec-1"}]
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    embedder = MagicMock()
    embedder.qdrant.delete = AsyncMock(side_effect=RuntimeError("qdrant down"))

    with pytest.raises(RuntimeError, match="Failed to clean old Qdrant vectors"):
        await run_process_pdf(
            paper_id=5,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
            force=True,
        )

    pdf_processor.process.assert_not_called()


@pytest.mark.asyncio
async def test_run_process_pdf_wraps_embedding_failures():
    """Embedding errors become a stable RuntimeError for callers."""
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    pool = _make_pool(conn)
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    embedder = MagicMock()

    with pytest.raises(RuntimeError, match="Embedding service error"):
        await run_process_pdf(
            paper_id=9,
            pdf_path=Path("/tmp/paper.pdf"),
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
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
