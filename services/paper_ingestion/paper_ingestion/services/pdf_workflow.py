"""Core PDF workflow utilities shared between routers and the scheduler.

Extracted from main.py so that the scheduler (which runs outside an HTTP
request context) can import these helpers without pulling in FastAPI
internals or causing circular imports.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import httpx
import torch
from qdrant_client.models import PointIdsList

from paper_ingestion.ingestion.embedder import COLLECTION_NAME, EMBEDDING_MODEL_NAME

if TYPE_CHECKING:
    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.models import PaperCreate
    from paper_ingestion.pdf_processor import PDFProcessor

logger = logging.getLogger(__name__)

ConnLike = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]

_EMBEDDING_ERROR_SECRET_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"(sk-[A-Za-z0-9._-]+)|"
    r"(Authorization:\s*)[^\s,;]+|"
    r"https?://[^\s,;]+",
    re.IGNORECASE,
)


def _sanitize_embedding_failure_detail(exc: BaseException, *, max_chars: int = 240) -> str:
    """Keep provider diagnostics actionable without leaking URLs or credentials."""
    compact = " ".join(str(exc).split())

    def _redact(match: re.Match[str]) -> str:
        if match.group(1) or match.group(3):
            return f"{match.group(1) or match.group(3)}<redacted>"
        if match.group(2):
            return "<redacted>"
        return "<url>"

    redacted = _EMBEDDING_ERROR_SECRET_RE.sub(_redact, compact)
    return redacted[:max_chars]


def _embedding_failure_message(exc: BaseException) -> str:
    detail = _sanitize_embedding_failure_detail(exc)
    base = detail if detail.lower().startswith("embedding service") else "Embedding service error"
    if detail and base != detail:
        base = f"{base}: {detail}"
    return (
        f"{base}. Check LiteLLM/Ollama health, embedding model availability, "
        "and LITELLM_MASTER_KEY wiring."
    )


# ---------------------------------------------------------------------------
# Advisory lock context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def advisory_lock(conn: ConnLike, lock_key: int, paper_id: int):
    """PostgreSQL advisory lock context manager."""
    await conn.execute("SELECT pg_advisory_lock($1, $2)", lock_key, paper_id)
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1, $2)", lock_key, paper_id)


# ---------------------------------------------------------------------------
# upsert_paper
# ---------------------------------------------------------------------------


async def upsert_paper(conn: ConnLike, paper: PaperCreate) -> asyncpg.Record:
    """Insert or update a paper, returning the row."""
    row = await conn.fetchrow(
        """INSERT INTO papers (external_id, source_type, title, authors, abstract,
                               published_date, url, pdf_url, citation_count, metadata,
                               discovery_origin)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
           ON CONFLICT (external_id) DO UPDATE SET
               title = EXCLUDED.title,
               authors = EXCLUDED.authors,
               abstract = EXCLUDED.abstract,
               citation_count = EXCLUDED.citation_count,
               metadata = EXCLUDED.metadata
           RETURNING *, (xmax = 0) AS is_insert""",
        paper.external_id,
        paper.source_type.value,
        paper.title,
        paper.authors,
        paper.abstract,
        paper.published_date,
        paper.url,
        paper.pdf_url,
        paper.citation_count,
        paper.metadata,
        paper.discovery_origin,
    )
    if row is None:
        raise RuntimeError("upsert_paper RETURNING always yields a row")
    return row


# ---------------------------------------------------------------------------
# run_process_pdf
# ---------------------------------------------------------------------------


async def run_process_pdf(
    paper_id: int,
    pdf_path: Path,
    db_pool: asyncpg.Pool,
    pdf_processor: PDFProcessor,
    embedder: Embedder,
    force: bool = False,
    ctx: object | None = None,
) -> dict:
    """Core PDF processing logic: idempotency check, embed, store.

    Splits work into three phases so the advisory lock is never held during
    the long-running embedding I/O (C-6):

    Phase 1 (under lock): DB idempotency check + optional force cleanup.
    Phase 2 (no lock):    Extract text, chunk, and embed (60 s+ I/O).
    Phase 3 (new conn):   Write chunks to DB with ON CONFLICT DO NOTHING.

    Used by both the ``process_pdf`` endpoint and the batch-process background
    task.  Raises ``RuntimeError`` (not ``HTTPException``) so callers without
    an HTTP context (e.g. the scheduler) can handle it appropriately; router
    callers should wrap ``RuntimeError`` in an ``HTTPException(502)``.

    Parameters
    ----------
    ctx:
        Optional job context (duck-typed: must have ``update_progress(float,
        str | None)`` coroutine).  When provided, reports progress at key
        milestones: 0.1 "Downloaded", 0.4 "Extracted", 0.7 "Chunked",
        0.7–1.0 per embedding batch, 1.0 "Done".
    """

    async def _maybe_progress(p: float, msg: str) -> None:
        if ctx is not None:
            await ctx.update_progress(p, msg)  # type: ignore[attr-defined]

    # --- Phase 1: idempotency check + DB cleanup under advisory lock ---
    # Qdrant delete intentionally moved outside the lock (see below) to avoid
    # holding the advisory lock during network I/O.
    point_ids_to_delete: list[str] = []
    async with db_pool.acquire() as conn:
        async with advisory_lock(conn, 1, paper_id):
            existing_count = await conn.fetchval(
                "SELECT COUNT(*) FROM paper_chunks WHERE paper_id = $1", paper_id
            )
            if existing_count > 0 and not force:
                await _maybe_progress(1.0, "Already processed")
                return {
                    "paper_id": paper_id,
                    "chunk_count": existing_count,
                    "status": "already_processed",
                }
            if existing_count > 0 and force:
                old_rows = await conn.fetch(
                    "SELECT embedding_id FROM paper_chunks "
                    "WHERE paper_id = $1 AND embedding_id IS NOT NULL",
                    paper_id,
                )
                await conn.execute("DELETE FROM paper_chunks WHERE paper_id = $1", paper_id)
                point_ids_to_delete = [r["embedding_id"] for r in old_rows]
    # Lock and connection released here.

    await _maybe_progress(0.1, "Downloaded")

    # Qdrant cleanup runs outside the advisory lock to avoid holding it during
    # network I/O.  DB rows are already deleted; if Qdrant delete fails, old
    # vectors become orphaned (harmless — they won't be matched by paper_id
    # filter during search).
    if point_ids_to_delete:
        try:
            await embedder.qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=point_ids_to_delete),  # type: ignore[arg-type]
            )
        except Exception as e:
            logger.error("Qdrant cleanup failed for paper %d: %s", paper_id, e)
            raise RuntimeError("Failed to clean old Qdrant vectors before reprocessing.") from e

    # --- Phase 2: Extract text, chunk, embed (no lock, no connection held) ---
    try:
        _full_text, chunks, point_ids = await pdf_processor.process(pdf_path, paper_id)
    except torch.OutOfMemoryError as e:
        logger.error("PDF text-extraction GPU OOM for paper %d: %s", paper_id, e)
        raise RuntimeError(
            "PDF text-extraction GPU out-of-memory. Lower OLLAMA_MAX_LOADED_MODELS"
            " (default 3 → try 2) or set TORCH_DEVICE=cpu for the paper_ingestion service."
        ) from e
    except RuntimeError as e:
        msg = str(e)
        if "CUDA out of memory" in msg or "CUDA error" in msg:
            logger.error("PDF text-extraction CUDA error for paper %d: %s", paper_id, e)
            raise RuntimeError(
                "PDF text-extraction GPU error. Lower OLLAMA_MAX_LOADED_MODELS or"
                " set TORCH_DEVICE=cpu."
            ) from e
        logger.error("Process PDF embedding failure for paper %d: %s", paper_id, e)
        raise RuntimeError(_embedding_failure_message(e)) from e
    except httpx.HTTPStatusError as e:
        logger.error("Process PDF embedding HTTP failure for paper %d: %s", paper_id, e)
        raise RuntimeError(_embedding_failure_message(e)) from e

    await _maybe_progress(0.4, "Extracted")
    await _maybe_progress(0.7, "Chunked")

    # --- Phase 3: Store chunks in DB (new connection, no advisory lock needed) ---
    # ON CONFLICT DO NOTHING handles the rare race where two requests both
    # passed phase 1.
    batch_size = 32
    total_batches = max((len(chunks) + batch_size - 1) // batch_size, 1)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """INSERT INTO paper_chunks (paper_id, chunk_index, content, page_number,
                                             start_char, end_char, embedding_id,
                                             embedding_model)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (paper_id, chunk_index) DO NOTHING""",
                [
                    (
                        paper_id,
                        chunk.chunk_index,
                        chunk.content,
                        chunk.page_number,
                        chunk.start_char,
                        chunk.end_char,
                        point_id,
                        EMBEDDING_MODEL_NAME,
                    )
                    for chunk, point_id in zip(chunks, point_ids)
                ],
            )

    # Report embedding progress (single batch for now since executemany is atomic)
    await _maybe_progress(0.7 + 0.3 * (1 / total_batches), f"Embedding batch 1/{total_batches}")
    await _maybe_progress(1.0, "Done")

    return {"paper_id": paper_id, "chunk_count": len(chunks), "status": "processed"}
