"""Core PDF workflow utilities shared between routers and the scheduler.

Extracted from main.py so that the scheduler (which runs outside an HTTP
request context) can import these helpers without pulling in FastAPI
internals or causing circular imports.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import httpx
from qdrant_client.models import PointIdsList

from app.embedder import COLLECTION_NAME, EMBEDDING_MODEL_NAME

if TYPE_CHECKING:
    from app.embedder import Embedder
    from app.models import PaperCreate
    from app.pdf_processor import PDFProcessor

logger = logging.getLogger(__name__)

ConnLike = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]


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


async def upsert_paper(conn: ConnLike, paper: "PaperCreate") -> asyncpg.Record:
    """Insert or update a paper, returning the row."""
    return await conn.fetchrow(
        """INSERT INTO papers (external_id, source_type, title, authors, abstract,
                               published_date, url, pdf_url, citation_count, metadata)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
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
    )


# ---------------------------------------------------------------------------
# run_process_pdf
# ---------------------------------------------------------------------------


async def run_process_pdf(
    paper_id: int,
    pdf_path: Path,
    db_pool: asyncpg.Pool,
    pdf_processor: "PDFProcessor",
    embedder: "Embedder",
    force: bool = False,
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
    """
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
                return {
                    "paper_id": paper_id,
                    "chunk_count": existing_count,
                    "status": "already_processed",
                }
            if existing_count > 0 and force:
                old_rows = await conn.fetch(
                    "SELECT embedding_id FROM paper_chunks WHERE paper_id = $1 AND embedding_id IS NOT NULL",
                    paper_id,
                )
                await conn.execute("DELETE FROM paper_chunks WHERE paper_id = $1", paper_id)
                point_ids_to_delete = [r["embedding_id"] for r in old_rows]
    # Lock and connection released here.

    # Qdrant cleanup runs outside the advisory lock to avoid holding it during
    # network I/O.  DB rows are already deleted; if Qdrant delete fails, old
    # vectors become orphaned (harmless — they won't be matched by paper_id
    # filter during search).
    if point_ids_to_delete:
        try:
            await embedder.qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=point_ids_to_delete),
            )
        except Exception as e:
            logger.error("Qdrant cleanup failed for paper %d: %s", paper_id, e)
            raise RuntimeError("Failed to clean old Qdrant vectors before reprocessing.") from e

    # --- Phase 2: Extract text, chunk, embed (no lock, no connection held) ---
    try:
        full_text, chunks, point_ids = await pdf_processor.process(pdf_path, paper_id)
    except (httpx.HTTPStatusError, RuntimeError) as e:
        logger.error("Process PDF embedding failure for paper %d: %s", paper_id, e)
        raise RuntimeError(
            "Embedding service error. Check that an LLM/embedding provider is configured."
        ) from e

    # --- Phase 3: Store chunks in DB (new connection, no advisory lock needed) ---
    # ON CONFLICT DO NOTHING handles the rare race where two requests both
    # passed phase 1.
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

    return {"paper_id": paper_id, "chunk_count": len(chunks), "status": "processed"}


# Backward-compatible aliases while internal imports are migrated.
_upsert_paper = upsert_paper
_run_process_pdf = run_process_pdf
