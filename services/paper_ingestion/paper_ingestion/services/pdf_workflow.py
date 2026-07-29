"""Core PDF workflow utilities shared between routers and the scheduler.

Extracted from main.py so that the scheduler (which runs outside an HTTP
request context) can import these helpers without pulling in FastAPI
internals or causing circular imports.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict, cast

import asyncpg
import httpx
from jarvis_common.library import is_in_library
from jarvis_common.paper_visibility import (
    PRIVATE_VISIBILITY_SCOPE,
    PUBLIC_VISIBILITY_SCOPE,
    VisibilityScope,
    require_verified_public_source,
)
from jarvis_common.paths import secure_path
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
    delete_paper_vectors,
)
from paper_ingestion.ingestion.payload_schema import (
    StaleVisibilityLeaseError,
    VectorVisibility,
    visibility_lease_is_current,
)
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.pdf_processor import (
    PDF_STORAGE_PATH,
    SNAPSHOT_STORAGE_PATH,
    pdf_publish_operation,
)

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
    """Raised when a paper row can no longer accept a downloaded PDF pointer."""


class PDFSourceSupersededError(RuntimeError):
    """Raised when a paper's source URL moves away from the one a run derived content from."""


class PDFRebuildNotPermittedError(RuntimeError):
    """Raised when a run that discards derived content cannot name a holding requester."""


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
            # `papers` before `paper_chunks`, matching every other transaction
            # that writes both. A promotion takes the paper row and then deletes
            # its chunk rows, so acquiring these in the opposite order lets the
            # two transactions wait on each other and deadlock.
            await conn.execute(
                "UPDATE papers SET chunked_at = now() WHERE id = $1",
                paper_id,
            )
            await conn.executemany(
                _UPDATE_RECONCILED_CHUNK_SQL,
                [
                    (paper_id, chunk.chunk_index, point_id, EMBEDDING_MODEL_NAME)
                    for chunk, point_id in zip(chunks, expected_ids)
                ],
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


# Raised instead of an embedding-service message when the run's content came from
# a source URL the paper no longer carries: the embedding failure is real but it
# is not why this run kept nothing, and there is no partial save to resume from.
_SUPERSEDED_SOURCE_MESSAGE = (
    "This paper's source changed while it was being processed, so this run's "
    "content was discarded. Process the paper again to use its current source."
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


# Conflict policies for the shared canonical upsert. Both are trusted code
# literals interpolated verbatim into the SQL — never request-derived input.
#
# _ATTACH_ONLY_CONFLICT: the unverified client path (batch-save, Zotero). On
# conflict it mutates nothing canonical; the no-op self-assignment returns the
# existing row unchanged while `(xmax = 0)` still reports is_insert correctly.
_ATTACH_ONLY_CONFLICT = "DO UPDATE SET external_id = papers.external_id"

# _TRUSTED_REFRESH_CONFLICT: the server-owned adapter path (search/pulse/
# auto-fetch, require_verified_public_source-guarded). On conflict the trusted
# adapter re-owns EVERY client-provided descriptive column from EXCLUDED and
# forces public scope. The assignments are unconditional (no COALESCE): a
# COALESCE would let a client-supplied value survive when the adapter's value
# is NULL. Columns not listed are preserved: `discovered_by` (insert-only audit
# identity), `discovery_origin` (a non-authorizing 4-value enum, immutable after
# insert), `external_id` (the conflict key), and the server-local processing
# columns absent from the INSERT list. Those processing columns and the paper's
# chunk rows describe content derived from the row's PREVIOUS `pdf_url`, so this
# clause alone does not leave a promoted row self-consistent —
# `upsert_verified_public_paper` discards that derived content separately.
_TRUSTED_REFRESH_CONFLICT = (
    "DO UPDATE SET "
    "source_type = EXCLUDED.source_type, "
    "title = EXCLUDED.title, "
    "authors = EXCLUDED.authors, "
    "abstract = EXCLUDED.abstract, "
    "published_date = EXCLUDED.published_date, "
    "url = EXCLUDED.url, "
    "pdf_url = EXCLUDED.pdf_url, "
    "citation_count = EXCLUDED.citation_count, "
    "metadata = EXCLUDED.metadata, "
    "visibility_scope = 'public'"
)


async def _run_paper_upsert(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
    visibility_scope: VisibilityScope,
    on_conflict: str,
) -> asyncpg.Record:
    """Execute the shared canonical paper upsert with a caller-selected conflict policy.

    Parameters
    ----------
    conn : ConnLike
        Active database connection or compatible transaction proxy.
    paper : PaperCreate
        Validated descriptive paper metadata inserted for a new row.
    discovered_by : int | None
        Optional audit identity recorded only on initial insertion.
    visibility_scope : VisibilityScope
        Scope written for a NEW row. The scope of an EXISTING row on conflict is
        governed by *on_conflict*, not by this value.
    on_conflict : str
        Trusted ``ON CONFLICT (external_id)`` action clause, interpolated
        verbatim into the SQL. It MUST be a module-level code literal, never
        request-derived input: either :data:`_ATTACH_ONLY_CONFLICT` (mutate
        nothing canonical on the existing row) or :data:`_TRUSTED_REFRESH_CONFLICT`
        (re-own the full descriptive surface and force public scope).

    Returns
    -------
    asyncpg.Record
        The inserted or existing row, including ``(xmax = 0) AS is_insert``.
    """
    row = await conn.fetchrow(
        f"""INSERT INTO papers (external_id, source_type, title, authors, abstract,
                               published_date, url, pdf_url, citation_count, metadata,
                               discovery_origin, discovered_by, visibility_scope)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
           ON CONFLICT (external_id) {on_conflict}
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
    On conflict this path is attach-only: it mutates no canonical column of the
    existing shared row — neither content nor scope. Callers that want a user to
    access an existing row must add `user_library` membership.
    """
    return await _run_paper_upsert(
        conn,
        paper,
        discovered_by=discovered_by,
        visibility_scope=PRIVATE_VISIBILITY_SCOPE,
        on_conflict=_ATTACH_ONLY_CONFLICT,
    )


# Pre-image: the source URL as it stood inside the promotion transaction,
# read before the upsert overwrites it.
_PRE_PROMOTION_STATE_SQL = "SELECT pdf_url FROM papers WHERE external_id = $1 FOR UPDATE"
_DELETE_DERIVED_CHUNKS_SQL = "DELETE FROM paper_chunks WHERE paper_id = $1"
# `is_insert` is re-projected so the returned record keeps the shape callers get
# from the upsert itself; this statement only ever runs on an existing row.
_RESET_DERIVED_PDF_STATE_SQL = (
    "UPDATE papers "
    "SET pdf_downloaded = FALSE, pdf_local_path = NULL, chunked_at = NULL, "
    "content_generation = content_generation + 1 "
    "WHERE id = $1 "
    "RETURNING *, false AS is_insert"
)


def _promotion_supersedes_derived_content(
    prior: asyncpg.Record | None,
    promoted: asyncpg.Record,
    pdf_url: str | None,
) -> bool:
    """Report whether a promotion left the row's PDF-derived content out of date.

    Parameters
    ----------
    prior : asyncpg.Record | None
        ``pdf_url`` read in the same transaction immediately before the upsert,
        or ``None`` if no row existed then.
    promoted : asyncpg.Record
        Row returned by the upsert, carrying ``is_insert``.
    pdf_url : str | None
        Source URL the trusted adapter has just written.

    Returns
    -------
    bool
        True when the derived content must be discarded.

    Notes
    -----
    A fresh insert has no derived content. The four verified adapters either
    emit no PDF URL or one concrete document URL. In particular, arXiv strips
    the revision from its canonical external id while retaining it in the PDF
    URL; Semantic Scholar and OpenAlex can move the selected open-access
    location. None supplies a content fingerprint, so a changed non-empty URL
    must be treated as potentially different bytes rather than normalized away.
    A conflict with no pre-image is a concurrent insert: that row is
    microseconds old and has nothing derived to lose, so discarding is the
    cheap fail-closed answer.
    """
    if promoted["is_insert"]:
        return False
    if prior is None:
        return True
    return (prior["pdf_url"] or None) != (pdf_url or None)


def _deleted_row_count(command_status: str) -> int:
    """Return the row count carried by a PostgreSQL ``DELETE n`` command tag.

    Parameters
    ----------
    command_status : str
        Status string ``conn.execute`` returns for the last command it ran.

    Returns
    -------
    int
        Rows the statement removed, or ``0`` when the tag carries no count.
        The count is only ever logged, so an unrecognized tag must not raise.
    """
    _, _, count = command_status.rpartition(" ")
    return int(count) if count.isdigit() else 0


async def upsert_verified_public_paper(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
    discarded_content_ids: list[int] | None = None,
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
    discarded_content_ids : list[int] | None
        Optional collector. The paper's id is appended once the block below has
        exited after discarding its derived content, so the caller can reclaim
        the matching vector points and files outside this transaction with
        :func:`reclaim_discarded_paper_content`.

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
    boundary is also covered by tests. On conflict this path re-owns every
    client-provided descriptive column and forces public scope. Descriptive
    columns are all the conflict clause covers, so whenever a refresh replaces
    its ``pdf_url``, the content derived from the superseded URL is discarded
    here: the paper's ``paper_chunks`` rows, ``pdf_downloaded``,
    ``pdf_local_path`` and ``chunked_at``. The paper's content generation is
    incremented in that same transaction so existing highlights become stale
    at the exact commit that retires their source document. The returned record
    reports that reset, so a caller echoing it cannot publish a stale local PDF
    pointer.
    Qdrant vectors and the stored PDF file are not modified by this function;
    removing the chunk rows is what forces a re-process. Their storage is
    reclaimed by the caller through ``discarded_content_ids``, after the
    transaction that owns the refresh commits.

    Maintenance constraint: the trusted refresh and the chunk-row delete belong
    to the single transaction opened here, so both become visible in the same
    commit. Retrieval in :mod:`paper_ingestion.rag.streaming` serves only
    excerpts a stored chunk row still backs, and reads those keys on a
    connection of its own — before the vector round trip on the single-paper
    path, after it on the cross-paper one. Neither ordering carries the
    guarantee: it rests on there being no committed state in which the new
    source URL is visible while its discarded chunk rows remain. Moving either
    statement into its own transaction — for example to shorten how long the
    row lock is held — creates that state and changes what those reads can
    observe.
    """
    require_verified_public_source(paper.source_type.value)
    discarded_id: int | None = None
    async with conn.transaction():
        prior = await conn.fetchrow(_PRE_PROMOTION_STATE_SQL, paper.external_id)
        row = await _run_paper_upsert(
            conn,
            paper,
            discovered_by=discovered_by,
            visibility_scope=PUBLIC_VISIBILITY_SCOPE,
            on_conflict=_TRUSTED_REFRESH_CONFLICT,
        )
        if _promotion_supersedes_derived_content(prior, row, paper.pdf_url):
            status = await conn.execute(_DELETE_DERIVED_CHUNKS_SQL, row["id"])
            logger.info(
                "Promotion discarded %d derived chunk row(s) for paper %d (%s)",
                _deleted_row_count(status),
                row["id"],
                row["external_id"],
            )
            reset = await conn.fetchrow(_RESET_DERIVED_PDF_STATE_SQL, row["id"])
            if reset is None:
                raise RuntimeError("derived-content reset RETURNING always yields a row")
            row, discarded_id = reset, int(reset["id"])
    # Recorded only after the block above ended, so an id never survives a
    # statement that the block then rolled back.
    if discarded_id is not None and discarded_content_ids is not None:
        discarded_content_ids.append(discarded_id)
    return row


_DISCARDED_CONTENT_STATE_SQL = (
    "SELECT pdf_local_path IS NULL AND chunked_at IS NULL AS discarded FROM papers WHERE id = $1"
)


async def _paper_content_is_still_discarded(conn: ConnLike, paper_id: int) -> bool:
    """Re-read whether *paper_id* still stores nothing derived from a PDF.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds; this issues no acquisition of its
        own, so it never runs inside a transaction that owns other work.
    paper_id : int
        Paper whose current state decides whether reclamation may proceed.

    Returns
    -------
    bool
        True only while the row still carries no stored PDF pointer and no
        chunking timestamp. Every other answer is False, so nothing is deleted
        on a premise this could not confirm.

    Notes
    -----
    The three ways the premise can fail mean different things to an operator and
    are logged apart. A row storing content again is the routine outcome the
    deferral makes likely. A read that fails leaves the storage to the next
    promotion. A row that has gone leaves its stored PDF, page images and vector
    points behind for good, because nothing will ask for them again. ``IS NULL``
    never evaluates to NULL, so a NULL answer identifies that absent row rather
    than unset columns.
    """
    try:
        discarded = await conn.fetchval(_DISCARDED_CONTENT_STATE_SQL, paper_id)
    except Exception:  # noqa: BLE001 — best-effort reclamation; an unconfirmed premise skips
        logger.warning(
            "Reclamation premise unreadable for paper %d; leaving its content in place",
            paper_id,
            exc_info=True,
        )
        return False
    if discarded is None:
        logger.warning(
            "Paper %d is gone; its stored PDF, page images and vector points are left behind",
            paper_id,
        )
        return False
    if not discarded:
        logger.info("Skipping reclamation for paper %d: it stores derived content again", paper_id)
        return False
    return True


async def _reclaim_stored_files(conn: ConnLike, paper_id: int) -> None:
    """Free the paper's stored PDF and page images under the publication lock.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds; the premise is re-read on it once
        the lock is taken.
    paper_id : int
        Paper whose stored files are being freed.

    Notes
    -----
    The lock is the one every PDF publisher holds across promoting
    ``{paper_id}.pdf`` and committing the pointer that names it, so no
    publication can land between the read below and these deletions. A
    publisher that has not started waits and republishes afterwards; one that
    has committed is seen by the read, which then removes nothing. Restore
    maintenance owns the same lock, and refusing it here leaves the files for a
    later promotion rather than deleting from a set being swapped.

    The caller already holds its connection when this lock is taken, which is
    the order every publisher uses, so neither can hold one while waiting for
    the other.

    The two steps are independent: a failure is logged and the next still runs.
    A re-derived document can be shorter than the one it replaces, so the image
    directory goes whole rather than page by page.
    """
    async with pdf_publish_operation(Path(PDF_STORAGE_PATH)):
        if not await _paper_content_is_still_discarded(conn, paper_id):
            return
        try:
            pdf_path = secure_path(PDF_STORAGE_PATH, f"{paper_id}.pdf")
            await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
        except Exception:  # noqa: BLE001 — best-effort; the file is unreferenced
            logger.warning("Stored PDF reclamation failed for paper %d", paper_id, exc_info=True)
        try:
            snapshot_dir = secure_path(SNAPSHOT_STORAGE_PATH, str(paper_id))
            await asyncio.to_thread(shutil.rmtree, snapshot_dir)
        except FileNotFoundError:
            pass  # no page images were ever rendered for this paper
        except Exception:  # noqa: BLE001 — best-effort; the images are unreferenced
            logger.warning("Page-image reclamation failed for paper %d", paper_id, exc_info=True)


async def _reclaim_discarded_paper_content_on_connection(conn: ConnLike, paper_id: int) -> None:
    """Free a paper's discarded PDF-derived content over the caller's connection.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds. A caller holding the per-paper
        mutation lock on it passes it here so the deletions run inside that
        lock.
    paper_id : int
        Paper whose derived content a promotion has just discarded.

    Notes
    -----
    Every step is best-effort: each failure is logged, the remaining steps still
    run, and nothing reaches the caller. That matters most to the locked caller,
    for which a raised cleanup failure would replace the error it has to report.

    The premise is re-read here rather than assumed from the caller's list. The
    discard leaves exactly the state the download sweep selects on, and the
    promotion has just written the source URL it would fetch, so the paper can
    acquire new content before this runs. The state is read rather than the
    source URL because it describes what these deletions destroy: a re-download
    keeps the promoted URL, so a URL comparison would still permit removing a
    file and page images the paper currently points at.

    One read cannot govern the whole call, because a vector-store round trip
    separates it from the file steps and a download can commit inside that gap.
    The file steps therefore re-read the premise while holding the lock every
    PDF publisher holds across promoting the file and committing the pointer
    that names it, which leaves no interleaving in which they remove a file or
    page images a committed download has just produced.

    The caller holds the per-paper mutation lock on *conn*, which excludes
    every writer of this paper's deterministic vectors for the whole call. An
    aborting run's points and a concurrent successful run's points use the same
    ids, so this serialization is what makes the delete unambiguous.
    """
    try:
        if not await _paper_content_is_still_discarded(conn, paper_id):
            return
        try:
            await delete_paper_vectors(paper_id)
        except Exception:  # noqa: BLE001 — best-effort; orphan vectors are unreachable
            logger.warning("Vector reclamation failed for paper %d", paper_id, exc_info=True)
        await _reclaim_stored_files(conn, paper_id)
    except Exception:  # noqa: BLE001 — best-effort; no failure may reach the caller
        logger.warning("Reclamation failed for paper %d", paper_id, exc_info=True)


async def reclaim_discarded_paper_content(paper_id: int, db_pool: asyncpg.Pool) -> None:
    """Free the storage a paper's discarded PDF-derived content left behind.

    Removes the paper's vector points, its stored PDF file and the directory of
    page images rendered from it.

    Parameters
    ----------
    paper_id : int
        Paper whose derived content a promotion has just discarded.
    db_pool : asyncpg.Pool
        Pool supplying the locked connection the reclamation reads and deletes on.

    Notes
    -----
    Reclamation, not a security control. Nothing here decides what a reader may
    see: the promotion removes the paper's ``paper_chunks`` rows in the same
    transaction as the visibility flip, and retrieval serves only excerpts a
    stored chunk row still backs, so anything left here is wasted space rather
    than reachable content. Every step is best-effort — each failure is logged,
    the remaining steps still run, and nothing reaches the caller.

    Call this only once the transaction that discarded the content has
    committed. Qdrant and the filesystem are not transactional, so running it
    inside that transaction would destroy content a rollback still points at.

    The per-paper mutation lock spans the state read, deterministic vector
    delete, and stored-file reclamation. A publisher that finishes first is
    observed as storing content again; a publisher that starts later waits and
    writes a fresh generation after reclamation releases the lock.
    """
    try:
        async with _paper_mutation_connection(db_pool, paper_id) as conn:
            await _reclaim_discarded_paper_content_on_connection(conn, paper_id)
    except Exception:  # noqa: BLE001 — best-effort; no failure may reach the caller
        logger.warning("Reclamation failed for paper %d", paper_id, exc_info=True)


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
    RuntimeError
        If text extraction or embedding fails. The HTTP router translates this
        service-level error; background callers can handle it directly.
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
        raise RuntimeError(
            f"{_embedding_failure_message(exc)} "
            f"({saved_chunk_count} chunks saved — retry to resume)."
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
