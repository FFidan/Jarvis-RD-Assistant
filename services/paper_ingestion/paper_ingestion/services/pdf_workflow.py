"""Core PDF workflow utilities shared between routers and the scheduler.

Extracted from main.py so that the scheduler (which runs outside an HTTP
request context) can import these helpers without pulling in FastAPI
internals or causing circular imports.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict, cast

import asyncpg
import httpx
from jarvis_common.paper_visibility import (
    PRIVATE_VISIBILITY_SCOPE,
    PUBLIC_VISIBILITY_SCOPE,
    VisibilityScope,
    require_verified_public_source,
)
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Condition,
    FieldCondition,
    Filter,
    FilterSelector,
    HasIdCondition,
    MatchValue,
    PointIdsList,
)

# torch is an optional GPU dependency: CPU-only / scheduler deployments must be
# able to import this module without it (same guard as ingestion.qwen3_reranker).
try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

from paper_ingestion.db_types import ConnLike
from paper_ingestion.ingestion.embed_store import (
    chunk_embedding_fingerprint,
    chunk_point_id,
)
from paper_ingestion.ingestion.embedder import (
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
    EmbeddingBatchError,
    EmbeddingRunContext,
)
from paper_ingestion.ingestion.payload_schema import (
    StaleVisibilityLeaseError,
    VectorVisibility,
    visibility_lease_is_current,
)
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.pdf_processor import pdf_publish_operation

if TYPE_CHECKING:
    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.models import PaperCreate
    from paper_ingestion.pdf_processor import PDFProcessor

logger = logging.getLogger(__name__)

_PAPER_LOCK_RETRY_INITIAL_SECONDS = 0.05
_PAPER_LOCK_RETRY_MAX_SECONDS = 1.0


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


class EmbeddingReconcileResult(TypedDict):
    """Outcome of checking persisted chunks against the active vector collection."""

    paper_id: int
    chunk_count: int
    status: Literal["empty", "healthy", "repaired"]


class ProcessPdfProgressContext(Protocol):
    """Duck-typed progress reporter accepted by :func:`run_process_pdf`."""

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        """Report fractional progress (0.0–1.0) with an optional status message."""
        ...


class PDFRecordMissingError(RuntimeError):
    """Raised when a paper disappears before its downloaded PDF can be recorded."""


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
        If the paper row disappears after the download was staged.

    Notes
    -----
    The restore-shared filesystem lock spans file promotion and database
    commit. Any database or publication failure restores the prior file, and
    the staged download is removed on every exit path.
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
                        "WHERE id = $2 RETURNING *",
                        str(final_path),
                        paper_id,
                    )
                    if updated is None:
                        raise PDFRecordMissingError(
                            f"Paper {paper_id} was deleted while its PDF was downloading"
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

_RECONCILE_PROBE_BATCH_SIZE = 128
_RECONCILE_MAX_ATTEMPTS = 3
_PERSISTED_CHUNKS_SQL = """\
SELECT c.id AS chunk_id, c.paper_id, c.chunk_index, c.content, c.page_number,
       c.start_char, c.end_char, c.embedding_id, c.embedding_model,
       p.source_type, p.visibility_scope, p.discovered_by
  FROM paper_chunks c
  JOIN papers p ON p.id = c.paper_id
 WHERE c.paper_id = $1
 ORDER BY c.chunk_index"""
_UPDATE_RECONCILED_CHUNK_SQL = """\
UPDATE paper_chunks
   SET embedding_id = $3, embedding_model = $4
 WHERE paper_id = $1 AND chunk_index = $2"""


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


def _persisted_chunk_snapshot(
    rows: list[Any],
) -> tuple[tuple[Any, ...], ...]:
    """Return the vector-bearing fields that must stay stable during reconciliation."""
    return tuple(
        (
            int(row["chunk_index"]),
            str(row["content"]),
            row["page_number"],
            row["start_char"],
            row["end_char"],
            row["source_type"],
            row["visibility_scope"],
            row["discovered_by"],
        )
        for row in rows
    )


def _vector_identity_matches_chunk(
    record: Any,
    paper_id: int,
    chunk_index: int,
    content: str,
) -> bool:
    """Return whether a point proves deterministic content and model identity."""
    payload = getattr(record, "payload", None)
    return (
        str(getattr(record, "id", "")) == chunk_point_id(paper_id, chunk_index)
        and isinstance(payload, dict)
        and payload.get("paper_id") == paper_id
        and payload.get("chunk_index") == chunk_index
        and payload.get("embedding_model") == EMBEDDING_MODEL_NAME
        and payload.get("embedding_fingerprint") == chunk_embedding_fingerprint(content)
    )


def _vector_visibility_matches(record: Any, visibility: VectorVisibility) -> bool:
    """Return whether a point carries the exact current authorization payload."""
    payload = getattr(record, "payload", None)
    return isinstance(payload, dict) and all(
        payload.get(key) == value for key, value in visibility.payload.items()
    )


def _visibility_for_row(row: Any, generation: str) -> VectorVisibility:
    """Build immutable vector authorization metadata from a persisted paper row."""
    return VectorVisibility(
        source_type=str(row["source_type"]),
        visibility_scope=cast(VisibilityScope, str(row["visibility_scope"])),
        visibility_generation=generation,
    )


async def _resolve_visibility_generation(
    embedder: Embedder,
    explicit_generation: str | None = None,
) -> str:
    """Return an explicit worker generation or resolve the deployment current value."""
    return explicit_generation or await embedder.current_visibility_generation()


async def _load_paper_embedding_context(
    conn: ConnLike,
    paper_id: int,
    generation: str,
) -> tuple[VectorVisibility, int | None]:
    """Load one paper's persisted vector visibility and legacy audit owner."""
    paper_row = await conn.fetchrow(
        "SELECT source_type, visibility_scope, discovered_by FROM papers WHERE id = $1",
        paper_id,
    )
    if paper_row is None:
        raise RuntimeError(f"Paper {paper_id} no longer exists")
    return _visibility_for_row(paper_row, generation), paper_row["discovered_by"]


async def _require_current_worker_lease(
    conn: ConnLike,
    *,
    generation: str,
    worker_lease_token: str | None,
) -> None:
    """Abort before mutation when a cross-paper worker has lost its lease."""
    if worker_lease_token is None:
        return
    if not await visibility_lease_is_current(
        conn,
        generation=generation,
        worker_token=worker_lease_token,
    ):
        raise StaleVisibilityLeaseError(
            "Vector visibility generation or reconciliation lease changed"
        )


async def _retrieve_vectors(
    embedder: Embedder,
    point_ids: list[str],
) -> list[Any]:
    """Retrieve vector identities, recreating a deleted collection once."""
    kwargs = {
        "collection_name": COLLECTION_NAME,
        "ids": point_ids,
        "with_payload": True,
        "with_vectors": False,
    }
    try:
        return list(await embedder.qdrant.retrieve(**kwargs))
    except UnexpectedResponse as exc:
        if exc.status_code != 404:
            raise
        embedder._collection_ensured = False
        await embedder.ensure_collection()
        return list(await embedder.qdrant.retrieve(**kwargs))


async def _delete_reconcile_generation(  # noqa: PLR0913 - fenced mutation inputs
    embedder: Embedder,
    paper_id: int,
    chunks: list[ChunkForEmbedding],
    *,
    conn: ConnLike,
    visibility_generation: str,
    worker_lease_token: str | None,
) -> None:
    """Delete only points still carrying this stale reconciliation generation.

    Point ID, content fingerprint, and visibility generation are evaluated
    together by Qdrant. A newer writer that replaces the deterministic point
    with the same content under a rotated generation therefore survives.
    """
    if not chunks:
        return
    await _require_current_worker_lease(
        conn,
        generation=visibility_generation,
        worker_lease_token=worker_lease_token,
    )
    generation_filters: list[Condition] = [
        Filter(
            must=[
                HasIdCondition(has_id=[chunk_point_id(paper_id, chunk.chunk_index)]),
                FieldCondition(
                    key="embedding_fingerprint",
                    match=MatchValue(value=chunk_embedding_fingerprint(chunk.content)),
                ),
                FieldCondition(
                    key="visibility_generation",
                    match=MatchValue(value=visibility_generation),
                ),
            ]
        )
        for chunk in chunks
    ]
    await embedder.qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=FilterSelector(filter=Filter(should=generation_filters)),
        wait=True,
    )


async def _reconcile_resume_content(  # noqa: PLR0913 - reconciliation inputs
    embedder: Embedder,
    conn: ConnLike,
    paper_id: int,
    rows: list[Any],
    expected_ids: dict[int, str],
    visibility: VectorVisibility,
    *,
    worker_lease_token: str | None,
) -> dict[int, str]:
    """Return reusable chunks after repairing authorization-only payload drift."""
    records_by_id: dict[str, Any] = {}
    point_ids = list(expected_ids.values())
    for offset in range(0, len(point_ids), _RECONCILE_PROBE_BATCH_SIZE):
        records = await _retrieve_vectors(
            embedder,
            point_ids[offset : offset + _RECONCILE_PROBE_BATCH_SIZE],
        )
        records_by_id.update((str(record.id), record) for record in records)
    reusable: dict[int, str] = {}
    payload_repairs: list[str] = []
    for row in rows:
        chunk_index = int(row["chunk_index"])
        expected_id = expected_ids[chunk_index]
        record = records_by_id.get(expected_id)
        identity_matches = (
            row["embedding_model"] == EMBEDDING_MODEL_NAME
            and str(row["embedding_id"]) == expected_id
            and _vector_identity_matches_chunk(
                record,
                paper_id,
                chunk_index,
                str(row["content"]),
            )
        )
        if not identity_matches:
            continue
        reusable[chunk_index] = str(row["content"])
        if not _vector_visibility_matches(record, visibility):
            payload_repairs.append(expected_id)
    if payload_repairs:
        await _require_current_worker_lease(
            conn,
            generation=visibility.visibility_generation,
            worker_lease_token=worker_lease_token,
        )
        await embedder.qdrant.set_payload(
            collection_name=COLLECTION_NAME,
            payload=visibility.payload,
            # qdrant-client models point IDs as invariant ExtendedPointId lists;
            # runtime validation retains the already-validated string IDs.
            points=PointIdsList(points=cast(Any, payload_repairs)),
            wait=True,
        )
    return reusable


async def _mark_reconcile_healthy(
    conn: ConnLike,
    paper_id: int,
    original_snapshot: tuple[tuple[Any, ...], ...],
) -> bool:
    """Mark a paper healthy after a locked snapshot check."""
    current_rows = list(await conn.fetch(_PERSISTED_CHUNKS_SQL, paper_id))
    changed = _persisted_chunk_snapshot(current_rows) != original_snapshot
    if not changed:
        await conn.execute(
            "UPDATE papers SET chunked_at = now() WHERE id = $1",
            paper_id,
        )
    return changed


async def _commit_reconciled_chunk_metadata(
    conn: ConnLike,
    paper_id: int,
    chunks: list[ChunkForEmbedding],
    expected_ids: list[str],
    original_snapshot: tuple[tuple[Any, ...], ...],
) -> bool:
    """Commit repaired metadata only if the locked persisted snapshot is unchanged."""
    async with conn.transaction():
        current_rows = list(await conn.fetch(_PERSISTED_CHUNKS_SQL, paper_id))
        changed = _persisted_chunk_snapshot(current_rows) != original_snapshot
        if not changed:
            await conn.executemany(
                _UPDATE_RECONCILED_CHUNK_SQL,
                [
                    (paper_id, chunk.chunk_index, point_id, EMBEDDING_MODEL_NAME)
                    for chunk, point_id in zip(chunks, expected_ids)
                ],
            )
            await conn.execute(
                "UPDATE papers SET chunked_at = now() WHERE id = $1",
                paper_id,
            )
    return changed


def _reconciliation_changed_error(paper_id: int) -> RuntimeError:
    """Return the stable error raised after reconciliation's bounded retries."""
    return RuntimeError(f"Paper {paper_id} chunks kept changing during embedding reconciliation")


async def _reconcile_paper_embeddings_locked(
    paper_id: int,
    conn: ConnLike,
    embedder: Embedder,
    *,
    visibility_generation: str,
    worker_lease_token: str | None = None,
) -> EmbeddingReconcileResult:
    """Repair vectors while the caller holds the paper's mutation lock.

    The database is the durable source for chunk content. Qdrant probes use only
    deterministic point IDs and are capped per request. A probe failure propagates
    so schedulers can retry it; it is never interpreted as a healthy paper. Chunk
    metadata is advanced to the current model only after every required upsert has
    succeeded and an optimistic re-read confirms the chunks did not change.
    """
    for attempt in range(1, _RECONCILE_MAX_ATTEMPTS + 1):
        rows = list(await conn.fetch(_PERSISTED_CHUNKS_SQL, paper_id))
        if not rows:
            return {"paper_id": paper_id, "chunk_count": 0, "status": "empty"}
        original_snapshot = _persisted_chunk_snapshot(rows)
        visibility = _visibility_for_row(rows[0], visibility_generation)

        expected_ids = {
            int(row["chunk_index"]): chunk_point_id(paper_id, int(row["chunk_index"]))
            for row in rows
        }
        resume_content = await _reconcile_resume_content(
            embedder,
            conn,
            paper_id,
            rows,
            expected_ids,
            visibility,
            worker_lease_token=worker_lease_token,
        )
        if len(resume_content) == len(rows):
            changed = await _mark_reconcile_healthy(conn, paper_id, original_snapshot)
            if changed:
                if attempt == _RECONCILE_MAX_ATTEMPTS:
                    raise _reconciliation_changed_error(paper_id)
                continue
            return {"paper_id": paper_id, "chunk_count": len(rows), "status": "healthy"}

        chunks = [
            ChunkForEmbedding(
                chunk_index=int(row["chunk_index"]),
                content=str(row["content"]),
                page_number=row["page_number"],
                start_char=int(row["start_char"] or 0),
                end_char=int(
                    row["end_char"] if row["end_char"] is not None else len(row["content"])
                ),
            )
            for row in rows
        ]
        written_chunks = [
            chunk for chunk in chunks if resume_content.get(chunk.chunk_index) != chunk.content
        ]
        await _require_current_worker_lease(
            conn,
            generation=visibility_generation,
            worker_lease_token=worker_lease_token,
        )
        stored_ids = await embedder.embed_and_store(
            paper_id,
            chunks,
            user_id=rows[0]["discovered_by"],
            visibility=visibility,
            run_context=EmbeddingRunContext(resume_content=resume_content),
        )
        expected_in_order = [expected_ids[chunk.chunk_index] for chunk in chunks]
        if [str(point_id) for point_id in stored_ids] != expected_in_order:
            await _delete_reconcile_generation(
                embedder,
                paper_id,
                written_chunks,
                conn=conn,
                visibility_generation=visibility_generation,
                worker_lease_token=worker_lease_token,
            )
            raise RuntimeError(
                "Embedding reconciliation returned unexpected point IDs for paper "
                f"{paper_id}; retry"
            )

        changed = await _commit_reconciled_chunk_metadata(
            conn,
            paper_id,
            chunks,
            expected_in_order,
            original_snapshot,
        )
        if changed:
            await _delete_reconcile_generation(
                embedder,
                paper_id,
                written_chunks,
                conn=conn,
                visibility_generation=visibility_generation,
                worker_lease_token=worker_lease_token,
            )
            if attempt == _RECONCILE_MAX_ATTEMPTS:
                raise _reconciliation_changed_error(paper_id)
            continue
        return {"paper_id": paper_id, "chunk_count": len(rows), "status": "repaired"}

    raise AssertionError("unreachable reconciliation attempt state")


async def reconcile_paper_embeddings(
    paper_id: int,
    db_pool: asyncpg.Pool,
    embedder: Embedder,
    *,
    visibility_generation: str | None = None,
    worker_lease_token: str | None = None,
) -> EmbeddingReconcileResult:
    """Repair stale or missing vectors under the shared per-paper lock.

    Parameters
    ----------
    paper_id : int
        Persisted paper whose chunk vectors must match PostgreSQL.
    db_pool : asyncpg.Pool
        Pool used to acquire the paper advisory lock and read chunk metadata.
    embedder : Embedder
        Vector storage facade and current-generation provider.
    visibility_generation : str | None
        Explicit generation owned by the cross-paper worker. Ordinary callers
        resolve the current generation from ``embedder``.
    worker_lease_token : str | None
        Lease paired with an explicit generation. It is checked under the
        paper lock immediately before every Qdrant mutation.

    Returns
    -------
    EmbeddingReconcileResult
        ``empty``, ``healthy``, or ``repaired`` with the persisted chunk count.

    Raises
    ------
    ValueError
        If exactly one of ``visibility_generation`` and ``worker_lease_token``
        is provided.
    StaleVisibilityLeaseError
        If a worker generation or lease changed before mutation.
    """
    if (visibility_generation is None) != (worker_lease_token is None):
        raise ValueError("Visibility generation and worker lease token must be supplied together")
    generation = await _resolve_visibility_generation(embedder, visibility_generation)
    async with _paper_mutation_connection(db_pool, paper_id) as conn:
        return await _reconcile_paper_embeddings_locked(
            paper_id,
            conn,
            embedder,
            visibility_generation=generation,
            worker_lease_token=worker_lease_token,
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


# ---------------------------------------------------------------------------
# Advisory lock context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def advisory_lock(conn: ConnLike, lock_key: int, paper_id: int):
    """Acquire a PostgreSQL session-level advisory lock and release on exit.

    Parameters
    ----------
    conn : ConnLike
        Active asyncpg connection or pool proxy.
    lock_key : int
        First key component (classifies the lock type, e.g. 1=process, 2=summarize).
    paper_id : int
        Second key component (paper DB ID); combined with *lock_key* forms the
        unique 64-bit advisory lock identifier.

    Notes
    -----
    Uses ``pg_advisory_lock`` (blocking) rather than ``pg_try_advisory_lock``.
    The paper-processing and reconciliation paths intentionally keep this
    per-paper lock across Qdrant I/O so deterministic point replacement and
    PostgreSQL metadata publication form one serialized generation. Different
    papers use different lock keys and continue concurrently.
    """
    await conn.execute("SELECT pg_advisory_lock($1, $2)", lock_key, paper_id)
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1, $2)", lock_key, paper_id)


@asynccontextmanager
async def _paper_mutation_connection(db_pool: asyncpg.Pool, paper_id: int):
    """Yield a pooled connection holding the shared per-paper mutation lock.

    A contended probe returns its connection to the pool before sleeping, so
    duplicate requests for one long-running PDF cannot consume every pool slot.
    Once acquired, the same connection and session-level lock span the complete
    Qdrant plus PostgreSQL publication.
    """
    retry_delay = _PAPER_LOCK_RETRY_INITIAL_SECONDS
    while True:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT pg_try_advisory_lock($1, $2) AS acquired",
                1,
                paper_id,
            )
            acquired = row is not None and bool(row["acquired"])
            if acquired:
                try:
                    yield conn
                finally:
                    unlock_task = asyncio.create_task(
                        conn.execute("SELECT pg_advisory_unlock($1, $2)", 1, paper_id)
                    )
                    try:
                        await asyncio.shield(unlock_task)
                    except asyncio.CancelledError:
                        await unlock_task
                        raise
                return
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, _PAPER_LOCK_RETRY_MAX_SECONDS)


# ---------------------------------------------------------------------------
# upsert_paper
# ---------------------------------------------------------------------------


async def _upsert_paper_with_visibility(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
    visibility_scope: VisibilityScope,
) -> asyncpg.Record:
    """Execute the shared canonical upsert with a caller-selected trusted scope."""
    row = await conn.fetchrow(
        """INSERT INTO papers (external_id, source_type, title, authors, abstract,
                               published_date, url, pdf_url, citation_count, metadata,
                               discovery_origin, discovered_by, visibility_scope)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
           ON CONFLICT (external_id) DO UPDATE SET
               source_type = CASE
                   WHEN EXCLUDED.visibility_scope = 'public' THEN EXCLUDED.source_type
                   ELSE papers.source_type
               END,
               title = EXCLUDED.title,
               authors = EXCLUDED.authors,
               abstract = EXCLUDED.abstract,
               citation_count = EXCLUDED.citation_count,
               metadata = EXCLUDED.metadata,
               visibility_scope = CASE
                   WHEN EXCLUDED.visibility_scope = 'public' THEN 'public'
                   ELSE papers.visibility_scope
               END
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
        discovered_by,
        visibility_scope,
    )
    if row is None:
        raise RuntimeError("upsert_paper RETURNING always yields a row")
    return row


async def upsert_paper(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
) -> asyncpg.Record:
    """Insert or update a private-by-default canonical paper.

    Parameters
    ----------
    conn : ConnLike
        Active database connection or compatible transaction proxy.
    paper : PaperCreate
        Validated descriptive paper metadata. Its source label does not grant
        public visibility.
    discovered_by : int | None
        Optional audit identity recorded only on initial insertion.

    Returns
    -------
    asyncpg.Record
        Inserted or updated canonical paper row.

    Notes
    -----
    On conflict, this path preserves an existing public scope. Callers that
    want a user to access a private row must add `user_library` membership.
    """
    return await _upsert_paper_with_visibility(
        conn,
        paper,
        discovered_by=discovered_by,
        visibility_scope=PRIVATE_VISIBILITY_SCOPE,
    )


async def upsert_verified_public_paper(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
) -> asyncpg.Record:
    """Insert or promote a paper verified by a server-owned scholarly adapter.

    Parameters
    ----------
    conn : ConnLike
        Active database connection or compatible transaction proxy.
    paper : PaperCreate
        Metadata returned by the configured server-side source adapter.
    discovered_by : int | None
        Optional audit identity recorded only on initial insertion.

    Returns
    -------
    asyncpg.Record
        Inserted or updated canonical paper row with public visibility.

    Raises
    ------
    ValueError
        If `paper.source_type` is not in the centralized verified-public
        adapter set.

    Notes
    -----
    Authenticated request payloads must use :func:`upsert_paper`. The source
    guard is defense in depth; call-site ownership of the trusted adapter
    boundary is also covered by tests.
    """
    require_verified_public_source(paper.source_type.value)
    return await _upsert_paper_with_visibility(
        conn,
        paper,
        discovered_by=discovered_by,
        visibility_scope=PUBLIC_VISIBILITY_SCOPE,
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

    Returns
    -------
    ProcessPdfResult
        Paper identifier, resulting chunk count, processing status, and any
        best-effort stale-vector cleanup warnings.

    Raises
    ------
    RuntimeError
        If text extraction or embedding fails. The HTTP router translates this
        service-level error; background callers can handle it directly.

    Notes
    -----
    The paper-scoped session advisory lock spans Qdrant writes and the matching
    PostgreSQL metadata commit. When ``ctx`` is supplied, progress is reported
    after download, extraction, chunking, embedding, persistence, and completion.
    """

    async with _paper_mutation_connection(db_pool, paper_id) as conn:
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
        if exc.completed_chunks:
            try:
                async with conn.transaction():
                    await _persist_chunk_rows(
                        conn,
                        paper_id,
                        exc.completed_chunks,
                        exc.completed_point_ids,
                    )
                logger.info(
                    "Persisted %d resumable chunks for paper %d before embedding failure",
                    len(exc.completed_chunks),
                    paper_id,
                )
            except Exception:
                logger.error(
                    "Failed to persist resumable chunks for paper %d", paper_id, exc_info=True
                )
        logger.error("Process PDF embedding failure for paper %d: %s", paper_id, exc)
        raise RuntimeError(
            f"{_embedding_failure_message(exc)} "
            f"({len(exc.completed_chunks)} chunks saved — retry to resume)."
        ) from exc
    except RuntimeError as exc:
        if torch is not None and isinstance(exc, torch.OutOfMemoryError):
            logger.error("PDF text-extraction GPU OOM for paper %d: %s", paper_id, exc)
            raise RuntimeError(
                "PDF text-extraction GPU out-of-memory. Lower OLLAMA_MAX_LOADED_MODELS"
                " (default 3 → try 2) or set TORCH_DEVICE=cpu for the paper_ingestion service."
            ) from exc
        message = str(exc)
        if "CUDA out of memory" in message or "CUDA error" in message:
            logger.error("PDF text-extraction CUDA error for paper %d: %s", paper_id, exc)
            raise RuntimeError(
                "PDF text-extraction GPU error. Lower OLLAMA_MAX_LOADED_MODELS or"
                " set TORCH_DEVICE=cpu."
            ) from exc
        logger.error("Process PDF embedding failure for paper %d: %s", paper_id, exc)
        raise RuntimeError(_embedding_failure_message(exc)) from exc
    except httpx.HTTPStatusError as exc:
        logger.error("Process PDF embedding HTTP failure for paper %d: %s", paper_id, exc)
        raise RuntimeError(_embedding_failure_message(exc)) from exc

    async with conn.transaction():
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
