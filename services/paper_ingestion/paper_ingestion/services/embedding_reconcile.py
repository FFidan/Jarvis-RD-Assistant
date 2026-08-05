"""Reconciliation of persisted chunk rows against the active vector collection.

The database is the durable source for chunk content; these helpers repair
stale, missing or authorization-drifted vectors under the shared per-paper
mutation lock, fenced by the cross-paper worker's visibility lease.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import asyncpg
from jarvis_common.paper_visibility import VisibilityScope
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

from paper_ingestion.db_types import ConnLike
from paper_ingestion.ingestion.embed_store import (
    chunk_embedding_fingerprint,
    chunk_point_id,
)
from paper_ingestion.ingestion.embedder import (
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
    EmbeddingRunContext,
)
from paper_ingestion.ingestion.payload_schema import (
    StaleVisibilityLeaseError,
    VectorVisibility,
    visibility_lease_is_current,
)
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.services.paper_locks import _paper_mutation_connection

if TYPE_CHECKING:
    from paper_ingestion.ingestion.embedder import Embedder


class EmbeddingReconcileResult(TypedDict):
    """Outcome of checking persisted chunks against the active vector collection."""

    paper_id: int
    chunk_count: int
    status: Literal["empty", "healthy", "repaired"]


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
