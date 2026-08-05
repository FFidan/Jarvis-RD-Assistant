"""Core PDF workflow utilities shared between routers and the scheduler.

Extracted from main.py so that the scheduler (which runs outside an HTTP
request context) can import these helpers without pulling in FastAPI
internals or causing circular imports.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict

import asyncpg
import httpx
from jarvis_common.library import is_in_library
from qdrant_client.models import PointIdsList

# torch is an optional GPU dependency: CPU-only / scheduler deployments must be
# able to import this module without it (same guard as ingestion.qwen3_reranker).
try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

from paper_ingestion.db_types import ConnLike
from paper_ingestion.ingestion.embed_store import chunk_point_id
from paper_ingestion.ingestion.embedder import (
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
    EmbeddingBatchError,
)
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.pdf_processor import pdf_publish_operation

# Split into sibling service modules; re-exported names keep every existing
# import of ``paper_ingestion.services.pdf_workflow`` resolving.
from paper_ingestion.services.embedding_reconcile import (
    _PERSISTED_CHUNKS_SQL,
    EmbeddingReconcileResult,  # noqa: F401  # re-export
    _delete_reconcile_generation,  # noqa: F401  # re-export
    _load_paper_embedding_context,
    _reconcile_paper_embeddings_locked,
    _reconcile_resume_content,
    _resolve_visibility_generation,
    reconcile_paper_embeddings,  # noqa: F401  # re-export
)
from paper_ingestion.services.paper_content_reclaim import (
    _DISCARDED_CONTENT_STATE_SQL,  # noqa: F401  # re-export
    _reclaim_discarded_paper_content_on_connection,
    reclaim_discarded_paper_content,  # noqa: F401  # re-export
)
from paper_ingestion.services.paper_locks import (
    _PAPER_LOCK_MAX_WAIT_SECONDS,  # noqa: F401  # re-export
    _paper_mutation_connection,
    advisory_lock,  # noqa: F401  # re-export
    paper_locked_error,  # noqa: F401  # re-export
)
from paper_ingestion.services.paper_upsert import (
    _DELETE_DERIVED_CHUNKS_SQL,  # noqa: F401  # re-export
    upsert_paper,  # noqa: F401  # re-export
    upsert_verified_public_paper,  # noqa: F401  # re-export
)
from paper_ingestion.services.pdf_errors import (
    PDFRebuildNotPermittedError,
    PDFRecordMissingError,
    PDFSourceSupersededError,
    PDFUserFacingError,
)

if TYPE_CHECKING:
    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.pdf_processor import PDFProcessor

logger = logging.getLogger(__name__)


class ProcessPdfResult(TypedDict):
    """Return value from :func:`run_process_pdf`.

    Attributes
    ----------
    paper_id : int
        DB paper ID that was processed.
    chunk_count : int
        Number of chunks in the database after processing.
    status : str
        ``"already_processed"`` when persisted chunks and vectors are healthy;
        ``"processed"`` after extraction or vector repair.
    warnings : list[str]
        Optional; present only when a best-effort post-step partially failed
        (e.g. stale Qdrant vectors could not be deleted). The run itself still
        succeeded — DB chunk rows are authoritative.
    """

    paper_id: int
    chunk_count: int
    status: Literal["already_processed", "processed"]
    warnings: NotRequired[list[str]]


class ProcessPdfProgressContext(Protocol):
    """Duck-typed progress reporter accepted by :func:`run_process_pdf`."""

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        """Report fractional progress (0.0–1.0) with an optional status message."""
        ...


async def _require_rebuild_holdership(
    conn: ConnLike,
    paper_id: int,
    requester_id: int | None,
) -> None:
    """Require ``requester_id`` to hold ``paper_id`` before its content is discarded.

    Parameters
    ----------
    conn : ConnLike
        Connection already held for this run; reused rather than acquiring another.
    paper_id : int
        The paper whose derived content is about to be replaced.
    requester_id : int | None
        The caller a request-reachable path threads through. ``None`` means the
        caller could not name a requester at all.

    Raises
    ------
    PDFRebuildNotPermittedError
        When ``requester_id`` is ``None`` or the paper is absent from that
        user's library.

    Notes
    -----
    Fail-closed by design: an unnamed requester is refused rather than admitted.
    Read visibility is not enough here — every authenticated caller can see a
    public paper, but only a holder may discard the content everyone shares.
    """
    if requester_id is not None and await is_in_library(
        conn, user_id=requester_id, paper_id=paper_id
    ):
        return
    raise PDFRebuildNotPermittedError(
        f"Paper {paper_id} must be in your library before its content can be rebuilt"
    )


async def download_and_store_pdf(
    db_pool: asyncpg.Pool,
    pdf_processor: PDFProcessor,
    pdf_url: str,
    paper_id: int,
) -> asyncpg.Record:
    """Download a PDF and publish its file and database pointer atomically.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Pool used for the paper update transaction.
    pdf_processor : PDFProcessor
        Downloader that stages the PDF beside its final numeric path.
    pdf_url : str
        Source URL validated and downloaded by ``pdf_processor``.
    paper_id : int
        Existing paper row and final PDF filename identifier.

    Returns
    -------
    asyncpg.Record
        Updated paper row containing the committed local PDF pointer.

    Raises
    ------
    ValueError
        If the URL, file size, or publication paths fail validation.
    httpx.HTTPError
        If the remote PDF request fails.
    paper_ingestion.pdf_processor.PDFPublishBlockedError
        If restore maintenance begins before the file can be published.
    PDFRecordMissingError
        If the paper row disappears, or stops referencing ``pdf_url``, after
        the download was staged.

    Notes
    -----
    The restore-shared filesystem lock spans file promotion and database
    commit. Any database or publication failure restores the prior file, and
    the staged download is removed on every exit path.

    The pointer is published only while the row still references the URL that
    was actually fetched. A row whose ``pdf_url`` was replaced meanwhile would
    otherwise be given a flag and a path describing the previous source.
    """
    staged_path, final_path = await pdf_processor.stage_pdf_download(pdf_url, paper_id)
    try:
        # Acquire the DB connection before the filesystem lock. If restore
        # maintenance starts after the lock is granted, its connection revoke
        # aborts this transaction and the publication context removes the file
        # before restore can acquire the same lock for its set swap.
        async with db_pool.acquire() as conn:
            async with pdf_publish_operation(final_path.parent) as publication:
                async with conn.transaction():
                    await publication.promote(staged_path, final_path)
                    updated = await conn.fetchrow(
                        "UPDATE papers SET pdf_local_path = $1, pdf_downloaded = TRUE "
                        "WHERE id = $2 AND pdf_url = $3 RETURNING *",
                        str(final_path),
                        paper_id,
                        pdf_url,
                    )
                    if updated is None:
                        raise PDFRecordMissingError(
                            f"Paper {paper_id} was deleted, or no longer references this "
                            "URL, while its PDF was downloading"
                        )
        return updated
    finally:
        await asyncio.to_thread(staged_path.unlink, missing_ok=True)


@dataclass(frozen=True)
class _LockedPdfProcessRequest:
    """Dependencies and options for one PDF run inside a paper mutation lock."""

    paper_id: int
    pdf_path: Path
    conn: ConnLike
    pdf_processor: PDFProcessor
    embedder: Embedder
    force: bool
    ctx: ProcessPdfProgressContext | None


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


_INSERT_CHUNK_SQL = """\
INSERT INTO paper_chunks (paper_id, chunk_index, content, page_number,
                          start_char, end_char, embedding_id, embedding_model)
   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
   ON CONFLICT (paper_id, chunk_index) DO UPDATE
   SET content = EXCLUDED.content,
       page_number = EXCLUDED.page_number,
       start_char = EXCLUDED.start_char,
       end_char = EXCLUDED.end_char,
       embedding_id = EXCLUDED.embedding_id,
       embedding_model = EXCLUDED.embedding_model"""


async def _persist_chunk_rows(
    conn: ConnLike,
    paper_id: int,
    chunks: list[ChunkForEmbedding],
    point_ids: list[str],
) -> None:
    """Write chunk metadata rows, skipping any that already exist (idempotent)."""
    await conn.executemany(
        _INSERT_CHUNK_SQL,
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


_PAPER_SOURCE_URL_SQL = "SELECT pdf_url FROM papers WHERE id = $1"
# NULL (paper gone, or either column NULL) is falsy in Python, so an
# indeterminate premise refuses like a violated one.
_PAPER_PDF_READY_SQL = "SELECT pdf_downloaded AND pdf_local_path = $2 FROM papers WHERE id = $1"
# FOR UPDATE: without it the check would take a fresh READ COMMITTED statement
# snapshot that neither blocks on nor observes a promotion still holding the
# row's write lock, and that promotion could commit immediately afterwards.
# Blocking here instead makes the check read the promotion's committed version.
_LOCKED_PAPER_SOURCE_URL_SQL = "SELECT pdf_url FROM papers WHERE id = $1 FOR UPDATE"


async def _require_unchanged_source_url(
    conn: ConnLike, paper_id: int, source_url: str | None
) -> None:
    """Fail unless the paper still carries the source URL this run derived content from.

    Parameters
    ----------
    conn : ConnLike
        Connection holding the per-paper mutation lock and the commit
        transaction, so the row lock this takes is held until that commit.
    paper_id : int
        Paper whose current source URL is compared.
    source_url : str | None
        ``papers.pdf_url`` as read before processing began.

    Raises
    ------
    PDFSourceSupersededError
        If ``papers.pdf_url`` no longer equals ``source_url``.

    Notes
    -----
    ``pdf_url`` is compared rather than ``pdf_local_path`` because the local
    path is derived from the paper id alone, so every writer reproduces the
    same string: a promotion clears it, the pending-download sweep restores
    the identical value, and a comparison against it passes even though the
    content is stale. What ``pdf_url`` gives instead is a single writer on an
    existing row — the trusted promotion path's conflict clause — so no other
    code can move the value and no code at all can move it back mid-run. That
    is the whole claim: this check detects that the row's source changed while
    the run was working. It is neither necessary nor sufficient for the
    promotion's own decision to discard derived content, which is made
    separately and on different inputs.
    """
    current_url = await conn.fetchval(_LOCKED_PAPER_SOURCE_URL_SQL, paper_id)
    if current_url != source_url:
        raise PDFSourceSupersededError(
            f"Paper {paper_id} no longer carries the source URL this run processed; "
            "its chunks were discarded"
        )


def _embedding_failure_message(exc: BaseException) -> str:
    """Build a user-facing embedding failure message with redacted detail.

    Sanitizes URLs and credentials from the exception string, then appends
    a standard remediation hint.
    """
    detail = _sanitize_embedding_failure_detail(exc)
    base = detail if detail.lower().startswith("embedding service") else "Embedding service error"
    if detail and base != detail:
        base = f"{base}: {detail}"
    return (
        f"{base}. Check LiteLLM/Ollama health, embedding model availability, "
        "and LITELLM_MASTER_KEY wiring."
    )


# Raised instead of an embedding-service message when the run's content came from
# a source URL the paper no longer carries: the embedding failure is real but it
# is not why this run kept nothing, and there is no partial save to resume from.
_SUPERSEDED_SOURCE_MESSAGE = (
    "This paper's source changed while it was being processed, so this run's "
    "content was discarded. Process the paper again to use its current source."
)


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
    ctx: ProcessPdfProgressContext | None = None,
    *,
    requester_id: int | None = None,
) -> ProcessPdfResult:
    """Process a PDF while serializing its vector and database generation.

    Parameters
    ----------
    paper_id : int
        Database identifier of the paper to process.
    pdf_path : pathlib.Path
        Path to the downloaded PDF.
    db_pool : asyncpg.Pool
        Pool used for the paper-scoped advisory lock and metadata transaction.
    pdf_processor : PDFProcessor
        Extractor and chunking pipeline that writes the current vectors.
    embedder : Embedder
        Vector-store client used for reconciliation and stale-vector cleanup.
    force : bool, default=False
        Replace existing chunks and vectors instead of resuming or reconciling
        the current generation.
    ctx : ProcessPdfProgressContext | None, default=None
        Optional job progress reporter.
    requester_id : int | None, default=None
        Library owner on whose behalf a ``force`` run is made. Required for
        ``force``; ignored otherwise.

    Returns
    -------
    ProcessPdfResult
        Paper identifier, resulting chunk count, processing status, and any
        best-effort stale-vector cleanup warnings.

    Raises
    ------
    PDFUserFacingError
        If text extraction or embedding fails. The message is written for the
        requester: the HTTP router sanitizes it into its 502, and job callers
        translate it into a ``JobError`` so it reaches the job error payload.
    PDFSourceSupersededError
        If the paper does not reference ``pdf_path`` as its downloaded PDF when
        the run starts, or its ``pdf_url`` is replaced before the chunk commit,
        or before the partial save that an embedding failure attempts. A retry
        works from the current source URL, so it converges rather than repeating
        the same refusal.
    PDFRebuildNotPermittedError
        If ``force`` is set and ``requester_id`` is either absent or does not
        hold the paper in their library.

    Notes
    -----
    The paper-scoped session advisory lock spans Qdrant writes and the matching
    PostgreSQL metadata commit. When ``ctx`` is supplied, progress is reported
    after download, extraction, chunking, embedding, persistence, and completion.

    A run the commit fence rejects has its SQL rolled back, but not the vectors
    and page images it had already written. Those are reclaimed before the error
    leaves, while the lock still holds.

    This is the common chokepoint for ``force`` rebuilds, so the holdership rule
    on ``force`` holds for every route, job kind and enqueue path that reaches
    it. That is the scope of the rule: a paper's derived content is also
    discarded by :func:`upsert_verified_public_paper`, which promotes a row to
    public from server-owned adapter metadata and reads no requester.
    """

    async with _paper_mutation_connection(db_pool, paper_id) as conn:
        if force:
            await _require_rebuild_holdership(conn, paper_id, requester_id)
        try:
            return await _run_process_pdf_locked(
                _LockedPdfProcessRequest(
                    paper_id=paper_id,
                    pdf_path=pdf_path,
                    conn=conn,
                    pdf_processor=pdf_processor,
                    embedder=embedder,
                    force=force,
                    ctx=ctx,
                )
            )
        except PDFSourceSupersededError:
            # Reclaim on this connection: it still holds the per-paper lock, so
            # no other run can be writing the deterministic point ids being
            # removed. The cleanup never raises, so the caller still sees this.
            await _reclaim_discarded_paper_content_on_connection(conn, paper_id)
            raise


async def _run_process_pdf_locked(
    request: _LockedPdfProcessRequest,
) -> ProcessPdfResult:
    """Run the PDF workflow while the caller holds the paper mutation lock."""

    paper_id = request.paper_id
    pdf_path = request.pdf_path
    conn = request.conn
    pdf_processor = request.pdf_processor
    embedder = request.embedder
    force = request.force
    ctx = request.ctx

    async def _maybe_progress(p: float, msg: str) -> None:
        if ctx is not None:
            await ctx.update_progress(p, msg)

    point_ids_to_delete: list[str] = []
    # Both reads happen under the per-paper lock and before any content is
    # derived. The URL comes first so that a promotion landing between the two
    # is caught by the premise read: a promotion that discards derived content
    # also clears both of these columns, and nothing but a download can restore
    # them — which the download fence permits only against the row's current
    # pdf_url. A promotion landing after both reads is caught at commit.
    source_url = await conn.fetchval(_PAPER_SOURCE_URL_SQL, paper_id)
    if not await conn.fetchval(_PAPER_PDF_READY_SQL, paper_id, str(pdf_path)):
        raise PDFSourceSupersededError(
            f"Paper {paper_id} does not reference the PDF this run was asked to process; "
            "nothing was derived from it"
        )
    visibility_generation = await _resolve_visibility_generation(embedder)
    visibility, owner_id = await _load_paper_embedding_context(
        conn,
        paper_id,
        visibility_generation,
    )
    existing_count = int(
        await conn.fetchval("SELECT COUNT(*) FROM paper_chunks WHERE paper_id = $1", paper_id) or 0
    )
    chunked_at = None
    if existing_count > 0:
        chunked_at = await conn.fetchval("SELECT chunked_at FROM papers WHERE id = $1", paper_id)
    if existing_count > 0 and chunked_at is not None and not force:
        reconciled = await _reconcile_paper_embeddings_locked(
            paper_id,
            conn,
            embedder,
            visibility_generation=visibility_generation,
        )
        await _maybe_progress(
            1.0,
            "Repaired embeddings" if reconciled["status"] == "repaired" else "Already processed",
        )
        return {
            "paper_id": paper_id,
            "chunk_count": reconciled["chunk_count"],
            "status": "processed" if reconciled["status"] == "repaired" else "already_processed",
        }
    if existing_count > 0 and force:
        old_rows = await conn.fetch(
            "SELECT embedding_id FROM paper_chunks "
            "WHERE paper_id = $1 AND embedding_id IS NOT NULL",
            paper_id,
        )
        point_ids_to_delete = [r["embedding_id"] for r in old_rows]
    await _maybe_progress(0.1, "Downloaded")
    resume_content: dict[int, str] = {}
    if not force:
        prior_rows = list(await conn.fetch(_PERSISTED_CHUNKS_SQL, paper_id))
        if prior_rows:
            expected_ids = {
                int(row["chunk_index"]): chunk_point_id(
                    paper_id,
                    int(row["chunk_index"]),
                )
                for row in prior_rows
            }
            resume_content = await _reconcile_resume_content(
                embedder,
                conn,
                paper_id,
                prior_rows,
                expected_ids,
                visibility,
                worker_lease_token=None,
            )

    async def _process_progress(
        phase: Literal["extracted", "chunked", "embedding"],
        completed: int,
        total: int,
    ) -> None:
        if phase == "extracted":
            await _maybe_progress(0.3, "Extracted")
        elif phase == "chunked":
            await _maybe_progress(0.5, "Chunked")
        elif total > 0:
            fraction = min(max(completed / total, 0.0), 1.0)
            await _maybe_progress(
                0.5 + 0.4 * fraction,
                f"Embedding batch {completed}/{total}",
            )

    try:
        _full_text, chunks, point_ids = await pdf_processor.process(
            pdf_path,
            paper_id,
            user_id=owner_id,
            visibility=visibility,
            progress_callback=_process_progress if ctx is not None else None,
            resume_content=resume_content,
        )
        point_ids_to_delete = list(set(point_ids_to_delete) - set(point_ids))
    except EmbeddingBatchError as exc:
        saved_chunk_count = 0
        source_superseded = False
        if exc.completed_chunks:
            try:
                async with conn.transaction():
                    await _require_unchanged_source_url(conn, paper_id, source_url)
                    await _persist_chunk_rows(
                        conn,
                        paper_id,
                        exc.completed_chunks,
                        exc.completed_point_ids,
                    )
                saved_chunk_count = len(exc.completed_chunks)
                logger.info(
                    "Persisted %d resumable chunks for paper %d before embedding failure",
                    len(exc.completed_chunks),
                    paper_id,
                )
            except PDFSourceSupersededError:
                source_superseded = True
                logger.warning(
                    "Discarded %d resumable chunks for paper %d: it no longer carries"
                    " the source URL this run processed",
                    len(exc.completed_chunks),
                    paper_id,
                )
            except Exception:
                logger.error(
                    "Failed to persist resumable chunks for paper %d", paper_id, exc_info=True
                )
        logger.error("Process PDF embedding failure for paper %d: %s", paper_id, exc)
        if source_superseded:
            # The run is void whatever the embedding service did: its content came
            # from a source URL the paper no longer carries, so nothing was kept
            # and there is nothing for a resume to pick up. Raised as the
            # superseded type so this exit reaches the same reclamation handler as
            # a run the commit fence rejects; the caller sees the message either
            # way, and both types are RuntimeError.
            raise PDFSourceSupersededError(_SUPERSEDED_SOURCE_MESSAGE) from exc
        raise PDFUserFacingError(
            f"{_embedding_failure_message(exc)} "
            f"({saved_chunk_count} chunks saved — retry to resume)."
        ) from exc
    except RuntimeError as exc:
        if torch is not None and isinstance(exc, torch.OutOfMemoryError):
            logger.error("PDF text-extraction GPU OOM for paper %d: %s", paper_id, exc)
            raise PDFUserFacingError(
                "PDF text-extraction GPU out-of-memory. Lower OLLAMA_MAX_LOADED_MODELS"
                " (default 3 → try 2) or set TORCH_DEVICE=cpu for the paper_ingestion service."
            ) from exc
        message = str(exc)
        if "CUDA out of memory" in message or "CUDA error" in message:
            logger.error("PDF text-extraction CUDA error for paper %d: %s", paper_id, exc)
            raise PDFUserFacingError(
                "PDF text-extraction GPU error. Lower OLLAMA_MAX_LOADED_MODELS or"
                " set TORCH_DEVICE=cpu."
            ) from exc
        logger.error("Process PDF embedding failure for paper %d: %s", paper_id, exc)
        raise PDFUserFacingError(_embedding_failure_message(exc)) from exc
    except httpx.HTTPStatusError as exc:
        logger.error("Process PDF embedding HTTP failure for paper %d: %s", paper_id, exc)
        raise PDFUserFacingError(_embedding_failure_message(exc)) from exc

    async with conn.transaction():
        await _require_unchanged_source_url(conn, paper_id, source_url)
        if force:
            await conn.execute("DELETE FROM paper_chunks WHERE paper_id = $1", paper_id)
        await _persist_chunk_rows(conn, paper_id, chunks, point_ids)
        await conn.execute("UPDATE papers SET chunked_at = now() WHERE id = $1", paper_id)
    await _maybe_progress(0.95, "Saved chunks")

    cleanup_warnings: list[str] = []
    if point_ids_to_delete:
        try:
            await embedder.qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=point_ids_to_delete),  # type: ignore[arg-type]
            )
        except Exception as exc:
            logger.error("Qdrant cleanup failed for paper %d: %s", paper_id, exc, exc_info=True)
            cleanup_warnings.append(
                f"Stale-vector cleanup failed: {len(point_ids_to_delete)} stale vector(s)"
                " may remain in Qdrant (DB chunk rows are authoritative; see service logs)."
            )

    await _maybe_progress(1.0, "Done")
    result: ProcessPdfResult = {
        "paper_id": paper_id,
        "chunk_count": len(chunks),
        "status": "processed",
    }
    if cleanup_warnings:
        result["warnings"] = cleanup_warnings
    return result
