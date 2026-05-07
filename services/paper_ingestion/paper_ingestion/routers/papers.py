"""Paper CRUD and metadata endpoints."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, JobCreateResponse, assert_paper_ownership, escape_like
from jarvis_common.auth import current_user_id_or_none
from jarvis_common.paper_state import (  # noqa: I001
    assert_paper_in_states as _assert_paper_in_states,
    trash_paper as _trash_paper,
    upsert_paper_user_state as _upsert_paper_user_state,
)

from paper_ingestion.converters import (
    row_to_chunk_response,
    row_to_paper_response,
    row_to_summary_response,
)
from paper_ingestion.deps import get_db_pool, get_optional_embedder, limiter
from paper_ingestion.ingestion.embedder import delete_paper_vectors
from paper_ingestion.models import (
    AnnotationsRequest,
    BulkActionRequest,
    FeedbackRequest,
    FeedbackResponse,
    FeedCountsResponse,
    MarkReadResponse,
    PaperBriefResponse,
    PaperCreate,
    PaperDetailResponse,
    PaperResponse,
    ProcessBatchRequest,
    RecentFeedback,
    SourceType,
    UserStateResponse,
)
from paper_ingestion.queries.predicates import VIEW_PREDICATES
from paper_ingestion.routers._paper_helpers import (
    _upsert_recommendation_feedback,
    _upsert_state_and_starred,
)
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/papers",
    tags=["papers"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


# ---------------------------------------------------------------------------
# GET /api/papers/brief
# ---------------------------------------------------------------------------


@router.get("/brief", response_model=list[PaperBriefResponse])
@limiter.limit("60/minute")
async def list_papers_brief(
    request: Request,
    search: str | None = Query(default=None, min_length=1),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return lightweight paper list for selector dropdowns."""
    caller_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        if search:
            rows = await conn.fetch(
                """SELECT id, title, source_type, published_date
                   FROM papers
                   WHERE title ILIKE '%' || $1 || '%' ESCAPE '\\'
                     AND (user_id IS NULL OR user_id IS NOT DISTINCT FROM $2)
                   ORDER BY created_at DESC
                   LIMIT 200""",
                escape_like(search),
                caller_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, title, source_type, published_date
                   FROM papers
                   WHERE (user_id IS NULL OR user_id IS NOT DISTINCT FROM $1)
                   ORDER BY created_at DESC
                   LIMIT 200""",
                caller_id,
            )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/papers
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PaperResponse])
@limiter.limit("60/minute")
async def list_papers(
    request: Request,
    view: str | None = Query(default=None, max_length=64),
    source_type: SourceType | None = None,
    topic_id: int | None = None,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_optional_embedder),
) -> list[PaperResponse]:
    """List papers with optional filters.

    When a search query ``q`` is provided, attempts hybrid BM25 + semantic
    search via Reciprocal Rank Fusion.  Falls back to BM25-only if the
    embedder or Qdrant is unavailable.

    Parameters
    ----------
    view : str | None
        Filter by named view (one of the keys in
        :data:`paper_ingestion.queries.predicates.VIEW_PREDICATES`).
        Unknown values raise 422.
    source_type : SourceType | None
        Filter by paper source.
    topic_id : int | None
        Filter by associated topic.
    q : str | None
        Search query (triggers hybrid search when set).
    limit, offset : int
        Pagination parameters.

    Returns
    -------
    list[PaperResponse]
        Matching papers ordered by relevance (when searching) or
        ``created_at DESC``.
    """
    from paper_ingestion.converters import hybrid_dict_to_paper_response

    user_id = await current_user_id_or_none(request)

    if view is not None and view not in VIEW_PREDICATES:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown view '{view}'. Valid views: {sorted(VIEW_PREDICATES.keys())}"),
        )

    # ------------------------------------------------------------------
    # Hybrid search path: q is set and no other filters are active
    # ------------------------------------------------------------------
    has_extra_filters = any([view, source_type, topic_id])
    if q and not has_extra_filters:
        if embedder is not None and embedder.qdrant is not None:
            try:
                hybrid_results = await embedder.hybrid_search(
                    q, db_pool, limit=limit, offset=offset
                )
                return [await hybrid_dict_to_paper_response(r, db_pool) for r in hybrid_results]
            except Exception:
                logger.warning(
                    "Hybrid search failed, falling back to BM25-only",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Standard / fallback BM25 query path
    # ------------------------------------------------------------------
    query = "SELECT p.* FROM papers p"
    joins: list[str] = []
    conditions: list[str] = []
    params: list = []

    if topic_id is not None:
        joins.append("JOIN paper_topics pt ON p.id = pt.paper_id")
        params.append(topic_id)
        conditions.append(f"pt.topic_id = ${len(params)}")

    if view is not None:
        # Bind the user_id so other users' state rows do not leak into the
        # predicate. Mirrors the LEFT JOIN pattern used by routers/feed.py.
        params.append(user_id)
        joins.append(
            "LEFT JOIN paper_user_state pus ON pus.paper_id = p.id"
            f" AND (${len(params)}::int IS NULL OR pus.user_id IS NOT DISTINCT FROM ${len(params)})"
        )
        conditions.append(VIEW_PREDICATES[view])

    if source_type is not None:
        params.append(source_type.value)
        conditions.append(f"p.source_type = ${len(params)}")

    if q:
        params.append(q)
        conditions.append(f"p.search_vector @@ plainto_tsquery('english', ${len(params)})")

    if joins:
        query += " " + " ".join(joins)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    params.extend([limit, offset])
    query += f" ORDER BY p.created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [row_to_paper_response(row) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/papers/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/{paper_id}", response_model=PaperDetailResponse)
@limiter.limit("60/minute")
async def get_paper_detail(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PaperDetailResponse:
    """Get a paper with its summary, chunks, user state, and most recent feedback.

    Parameters
    ----------
    paper_id : int
        Database paper ID.

    Returns
    -------
    PaperDetailResponse
        Paper, optional summary, chunks, user state, and most recent
        recommendation feedback (if any).
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        paper_row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
        if not paper_row:
            raise HTTPException(status_code=404, detail="Paper not found")

        summary_row = await conn.fetchrow(
            "SELECT * FROM paper_summaries WHERE paper_id = $1", paper_id
        )
        chunk_rows = await conn.fetch(
            "SELECT * FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index", paper_id
        )
        user_state_row = await conn.fetchrow(
            """SELECT COALESCE(state, 'inbox') AS state,
                      state_before_trash,
                      COALESCE(starred, FALSE) AS starred,
                      rating, user_notes,
                      COALESCE(flagged, FALSE) AS flagged,
                      updated_at
               FROM paper_user_state
               WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2
               LIMIT 1""",
            paper_id,
            user_id,
        )
        feedback_row = await conn.fetchrow(
            """SELECT signal, source, created_at
               FROM recommendation_feedback
               WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2
               ORDER BY created_at DESC LIMIT 1""",
            paper_id,
            user_id,
        )
        project_link_count = await conn.fetchval(
            "SELECT COUNT(*) FROM project_papers WHERE paper_id = $1",
            paper_id,
        )

    paper = row_to_paper_response(paper_row)
    summary = row_to_summary_response(summary_row) if summary_row else None
    chunks = [row_to_chunk_response(r) for r in chunk_rows]
    user_state = (
        UserStateResponse(
            state=user_state_row["state"],
            state_before_trash=user_state_row["state_before_trash"],
            starred=bool(user_state_row["starred"]),
            rating=user_state_row["rating"],
            user_notes=user_state_row["user_notes"],
            flagged=bool(user_state_row["flagged"]),
            updated_at=user_state_row["updated_at"],
        )
        if user_state_row
        else None
    )
    recent_feedback = (
        RecentFeedback(
            signal=feedback_row["signal"],
            source=feedback_row["source"],
            created_at=feedback_row["created_at"],
        )
        if feedback_row
        else None
    )
    has_project_links = bool(project_link_count)

    return PaperDetailResponse(
        paper=paper,
        summary=summary,
        chunks=chunks,
        user_state=user_state,
        recent_feedback=recent_feedback,
        has_project_links=has_project_links,
    )


# ---------------------------------------------------------------------------
# POST /api/papers/batch-save
# ---------------------------------------------------------------------------


@router.post("/batch-save", response_model=list[PaperResponse])
@limiter.limit("5/minute")
async def batch_save_papers(
    request: Request,
    papers: Annotated[list[PaperCreate], Body()],
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[PaperResponse]:
    """Upsert a list of papers to the database (by external_id)."""
    _ = request  # required by @limiter.limit; not used in body
    max_batch = 100
    if len(papers) > max_batch:
        raise HTTPException(400, f"Batch size cannot exceed {max_batch}")
    if not papers:
        return []
    results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper in papers:
                # Wave 1cd Task B6: stamp citation_batch origin (overrides
                # PaperCreate's "user_initiated" default — the batch endpoint
                # is the canonical citation-graph fan-out path).
                paper.discovery_origin = "citation_batch"
                row = await upsert_paper(conn, paper)
                results.append(row_to_paper_response(row))
    return results


@router.post("/{paper_id}/feedback", response_model=FeedbackResponse)
@limiter.limit("60/minute")
async def submit_feedback(
    request: Request,
    paper_id: int,
    body: FeedbackRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Record per-paper recommendation feedback.

    Writes to the ``recommendation_feedback`` table (one row per
    ``(paper_id, user_id, source)`` triple — repeat submissions overwrite
    the prior row).

    Recommendation feedback is only accepted for papers discovered by the
    system (``pulse``, ``recommender``, or ``citation_batch``).  User-initiated
    papers are kept out of recommendation training; use ``trash_and_reject`` for
    the atomic trash plus negative feedback path.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

        origin_row = await conn.fetchrow(
            "SELECT discovery_origin FROM papers WHERE id = $1",
            paper_id,
        )
        if origin_row is None:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")
        if origin_row["discovery_origin"] == "user_initiated":
            raise HTTPException(
                status_code=400,
                detail=(
                    "recommendation feedback is only valid for system-discovered papers; "
                    "user_initiated papers are excluded from recommendation training"
                ),
            )

        try:
            await _upsert_recommendation_feedback(
                conn,
                paper_id,
                user_id,
                body.signal,
                body.source,
                body.reason,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found") from e
    return FeedbackResponse(
        paper_id=paper_id,
        signal=body.signal,
        source=body.source,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# DELETE /api/papers/{paper_id}/feedback  — clear a feedback signal (W1.5 UX-E.1)
# ---------------------------------------------------------------------------


@router.delete("/{paper_id}/feedback", status_code=204)
@limiter.limit("60/minute")
async def delete_paper_feedback(
    request: Request,
    paper_id: int,
    source: Annotated[str, Query()],
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Delete a recommendation_feedback row for this paper+user+source triple.

    Idempotent — returns 204 regardless of whether a row was deleted.
    ``source`` must be supplied as a query parameter (e.g. ``?source=pulse_thumbs``).
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM recommendation_feedback"
            " WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2 AND source = $3",
            paper_id,
            user_id,
            source,
        )


# ---------------------------------------------------------------------------
# GET /api/papers/feed/counts  — 10 named views per spec §6
# ---------------------------------------------------------------------------


@router.get("/feed/counts", response_model=FeedCountsResponse)
@limiter.limit("60/minute")
async def get_feed_counts(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Return per-bucket paper counts for the current user (10 named views)."""
    user_id = await current_user_id_or_none(request)

    def _sum(view_key: str, alias: str) -> str:
        return (
            f"COALESCE(SUM(CASE WHEN {VIEW_PREDICATES[view_key]} "
            f"THEN 1 ELSE 0 END), 0)::int AS {alias}"
        )

    sql = f"""
        SELECT
            {_sum("inbox", "inbox")},
            {_sum("library", "library")},
            {_sum("reading_list", "reading_list")},
            {_sum("reading", "reading")},
            {_sum("done", "done")},
            {_sum("starred", "starred")},
            {_sum("trash", "trash")},
            {_sum("active", "active")},
            {_sum("kept", "kept")},
            {_sum("all_non_trash", "all_non_trash")}
          FROM papers p
          LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
            AND ($1::int IS NULL OR pus.user_id IS NOT DISTINCT FROM $1)
         WHERE p.user_id IS NOT DISTINCT FROM $1
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(sql, user_id)
    assert row is not None  # aggregate query always returns one row
    return FeedCountsResponse(
        inbox=row["inbox"],
        library=row["library"],
        reading_list=row["reading_list"],
        reading=row["reading"],
        done=row["done"],
        starred=row["starred"],
        trash=row["trash"],
        active=row["active"],
        kept=row["kept"],
        all_non_trash=row["all_non_trash"],
    )


# ---------------------------------------------------------------------------
# Lifecycle helpers — Phase A Wave 1ab
# ---------------------------------------------------------------------------


async def _assert_paper_in_state(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    state: str,
) -> None:
    """Raise 409 if the paper is not in the expected state.

    Treats a missing ``paper_user_state`` row as ``state='inbox'`` per spec §6.
    """
    current = await conn.fetchval(
        "SELECT COALESCE(state, 'inbox') FROM paper_user_state"
        " WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2",
        paper_id,
        user_id,
    )
    if current != state:
        raise HTTPException(
            status_code=409,
            detail=(f"Paper must be in state '{state}'; currently '{current or 'inbox'}'"),
        )


async def _restore_paper(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
) -> None:
    """Restore from trash: ``state := COALESCE(state_before_trash, 'inbox')``.

    Also clears ``state_before_trash`` so the field only carries meaning
    while the paper is in trash.

    Raises HTTPException(404) if no matching row was updated (paper not found
    or not in trash for this caller).
    """
    status = await conn.execute(
        """UPDATE paper_user_state
              SET state = COALESCE(state_before_trash, 'inbox'),
                  state_before_trash = NULL
            WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2""",
        paper_id,
        user_id,
    )
    # asyncpg returns e.g. "UPDATE 1" — extract the count
    updated = int(status.split()[-1]) if status else 0
    if updated == 0:
        raise HTTPException(status_code=404, detail="Paper not found or not in trash")


async def _apply_bulk_action(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    action: str,
    *,
    _hard_deleted_ids: list[int] | None = None,
) -> None:
    """Dispatch a single bulk action to the appropriate state mutation."""
    if action == "save":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="to_read")
    elif action == "skip":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="done")
    elif action == "trash":
        await _trash_paper(conn, paper_id, user_id)
    elif action == "mark_reading":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="reading")
    elif action == "mark_done":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="done")
    elif action == "restore":
        await _assert_paper_in_state(conn, paper_id, user_id, state="trash")
        await _restore_paper(conn, paper_id, user_id)
    elif action == "star":
        await _upsert_state_and_starred(conn, paper_id, user_id, starred=True)
    elif action == "unstar":
        await _upsert_state_and_starred(conn, paper_id, user_id, starred=False)
    elif action == "feedback_positive":
        await _upsert_recommendation_feedback(conn, paper_id, user_id, "positive", "feed_thumbs")
    elif action == "feedback_negative":
        await _upsert_recommendation_feedback(conn, paper_id, user_id, "negative", "feed_thumbs")
    elif action == "hard_delete":
        await _assert_paper_in_state(conn, paper_id, user_id, state="trash")
        # Caller (bulk_action_papers) already wraps each paper in a per-paper
        # SAVEPOINT (async with conn.transaction()), so no inner txn is needed.
        await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
        # Collect the ID for Qdrant cleanup OUTSIDE the transaction.
        # Callers that do not pass _hard_deleted_ids get the legacy inline
        # behaviour as a fallback (e.g. tests that call this directly).
        if _hard_deleted_ids is not None:
            _hard_deleted_ids.append(paper_id)
        else:
            try:
                await delete_paper_vectors(paper_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup; orphan vectors are harmless
                logger.exception(
                    "Qdrant cleanup failed for paper %d in bulk hard_delete; "
                    "vectors are now orphans",
                    paper_id,
                )
    else:
        raise ValueError(f"Unknown bulk action: {action}")


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/save  — Reading List
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/save", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def save_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Save a paper to the Reading List (``state := 'to_read'``)."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _assert_paper_in_states(
            conn, paper_id, user_id, allowed=("inbox", "done", "to_read", "reading")
        )
        await _upsert_state_and_starred(conn, paper_id, user_id, state="to_read")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/unsave  — revert to_read → inbox (W1.5 UX-E.2)
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/unsave", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def unsave_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, object]:
    """Revert a saved paper from the Reading List back to the Inbox (``state := 'inbox'``).

    Requires the paper to be in ``to_read`` state; raises 409 otherwise.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_state(conn, paper_id, user_id, state="to_read")
        await _upsert_state_and_starred(conn, paper_id, user_id, state="inbox")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/skip  — Inbox skip → done
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/skip", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def skip_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Skip a paper from the Inbox (``state := 'done'``)."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("inbox",))
        await _upsert_state_and_starred(conn, paper_id, user_id, state="done")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/reading  — start reading
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/reading", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def reading_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Mark a paper as currently being read (``state := 'reading'``)."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _assert_paper_in_states(
            conn, paper_id, user_id, allowed=("to_read", "reading", "done")
        )
        await _upsert_state_and_starred(conn, paper_id, user_id, state="reading")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/done  — finish reading
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/done", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def done_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Mark a paper as done (``state := 'done'``)."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _upsert_state_and_starred(conn, paper_id, user_id, state="done")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/star
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/star", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def star_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Set ``starred = TRUE``. Does not change reading state.

    Side effect: enqueues a ``zotero.push`` job iff all three conditions hold:
    1. The paper was not already starred (off→on transition).
    2. The paper is linked to at least one project (``project_papers`` row).
    3. ``zotero.auto_push_on_star`` is ``true`` in ``user_config``.

    The off→on transition is detected atomically via a CTE + RETURNING so that
    double-star calls (client retry, double-tap) do not double-enqueue.

    The enqueue runs OUTSIDE the connection block so ``zotero_push.defer_async``
    (which acquires its own pool connection) cannot deadlock against the
    connection we hold here. Failures to enqueue are logged but do not
    fail the star mutation itself (best-effort).
    """
    _ = request  # required by @limiter.limit; not used in body
    user_id = await current_user_id_or_none(request)
    was_new_star = False
    project_link_count = 0
    auto_push_on_star = False
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        # Atomically upsert starred=TRUE and detect the off→on transition.
        # A CTE snapshots the previous starred value before the upsert so we
        # can determine whether this is a genuine transition without a separate
        # pre-flight SELECT (which would have a TOCTOU race window).
        upsert_result = await _upsert_paper_user_state(
            conn, paper_id, user_id, on_conflict="update_starred_only"
        )
        if upsert_result is not None:
            was_new_star = bool(upsert_result["is_new_row"]) or not bool(
                upsert_result["prev_starred"]
            )
        project_link_count = (
            await conn.fetchval(
                "SELECT COUNT(*) FROM project_papers WHERE paper_id = $1",
                paper_id,
            )
            or 0
        )
        _cfg_value = await conn.fetchval(
            "SELECT value FROM user_config WHERE key = 'zotero.auto_push_on_star'",
        )
        auto_push_on_star = _cfg_value is True
    # Outside conn block: enqueue without holding the pool slot
    if was_new_star and project_link_count > 0 and auto_push_on_star:
        try:
            from jarvis_common.task_registry import KIND_TO_TASK

            await KIND_TO_TASK["zotero.push"].defer_async(
                job_id=str(uuid.uuid4()), user_id=user_id, paper_id=paper_id
            )
        except Exception:
            logger.exception("zotero.push enqueue failed for paper %d", paper_id)
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/unstar
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/unstar", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def unstar_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Set ``starred = FALSE``. Does not change reading state."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _upsert_state_and_starred(conn, paper_id, user_id, starred=False)
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/trash
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/trash", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def trash_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Move paper to Trash. Atomic: ``state_before_trash := state; state := 'trash'``."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _trash_paper(conn, paper_id, user_id)
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/restore
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/restore", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def restore_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Restore a paper from Trash to its prior state."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _assert_paper_in_state(conn, paper_id, user_id, state="trash")
        await _restore_paper(conn, paper_id, user_id)
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/trash_and_reject  — combined action (spec §4.4)
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/trash_and_reject", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def trash_and_reject_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Trash the paper AND record negative feedback (``source='dismiss_combined'``).

    Single transaction. The only combined action in the system per spec §4.4.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        async with conn.transaction():
            await _trash_paper(conn, paper_id, user_id)
            await _upsert_recommendation_feedback(
                conn,
                paper_id,
                user_id,
                "negative",
                "dismiss_combined",
            )
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/annotations  — rating / notes / flagged (spec §3.3)
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/annotations", response_model=UserStateResponse)
@limiter.limit("30/minute")
async def annotate_paper(
    request: Request,
    paper_id: int,
    body: AnnotationsRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Update subjective per-paper annotations (rating 1-5, user_notes, flagged).

    Partial updates: any field left as ``None`` is preserved on conflict.
    Returns the resulting :class:`UserStateResponse` so the frontend can
    refresh its local cache without a follow-up GET.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        try:
            row = await _upsert_paper_user_state(
                conn,
                paper_id,
                user_id,
                rating=body.rating,
                user_notes=body.user_notes,
                flagged=body.flagged,
                on_conflict="update_partial",
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found") from e
    assert row is not None  # RETURNING guarantees a row on success
    return UserStateResponse(**dict(row))


# ---------------------------------------------------------------------------
# DELETE /api/papers/{paper_id}  — hard delete (preserves WS-AH2 NEW-H2)
# ---------------------------------------------------------------------------


@router.delete("/{paper_id}")
@limiter.limit("10/minute")
async def hard_delete_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Permanently delete a trashed paper.

    Cascades through FK; Qdrant cleanup is best-effort.

    Order rationale (WS-AH2 NEW-H2 — load-bearing): if SQL ``DELETE`` fails,
    the txn rolls back and Qdrant is untouched (user retries cleanly). If
    SQL succeeds and Qdrant fails, vectors are orphaned (recoverable). The
    reverse order is data-loss-prone — do not collapse the inside-txn
    DELETE and outside-txn Qdrant cleanup into a single try/except.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_state(conn, paper_id, user_id, state="trash")
        async with conn.transaction():
            await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
        # Qdrant cleanup OUTSIDE the transaction — Qdrant is non-transactional;
        # we prefer the row to commit first so a Qdrant failure leaves orphan
        # vectors (recoverable) rather than a missing-vectors row (data loss).
        try:
            await delete_paper_vectors(paper_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.exception(
                "Qdrant cleanup failed for paper %d after DB delete; vectors are now orphans",
                paper_id,
            )
    return {"deleted": paper_id}


# ---------------------------------------------------------------------------
# POST /api/papers/bulk
# ---------------------------------------------------------------------------


@router.post("/bulk")
@limiter.limit("10/minute")
async def bulk_action_papers(
    request: Request,
    body: BulkActionRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Apply a lifecycle action to multiple papers atomically.

    Returns ``{"succeeded": [...], "failed": [{"paper_id": int, "error": str}]}``.
    Partial failures are collected; the outer transaction is committed even when
    individual papers fail (per-paper savepoints isolate rollbacks).
    """
    user_id = await current_user_id_or_none(request)
    succeeded: list[int] = []
    failed: list[dict[str, object]] = []
    # Track hard-deleted IDs for Qdrant cleanup OUTSIDE the transaction so that
    # Qdrant I/O does not block or deadlock inside a PostgreSQL SAVEPOINT.
    hard_deleted_ids: list[int] = []

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper_id in body.paper_ids:
                # Nested asyncpg transaction = SAVEPOINT — failure of one paper
                # rolls back only its savepoint, leaving the outer transaction
                # alive so subsequent papers can still commit.
                try:
                    async with conn.transaction():
                        await assert_paper_ownership(conn, paper_id, user_id)
                        await _apply_bulk_action(
                            conn,
                            paper_id,
                            user_id,
                            body.action,
                            _hard_deleted_ids=hard_deleted_ids,
                        )
                    succeeded.append(paper_id)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"paper_id": paper_id, "error": str(exc)})

    # Qdrant cleanup runs OUTSIDE the PostgreSQL transaction to avoid deadlock /
    # SAVEPOINT bloat.  Failures are logged but do not affect the HTTP response.
    for pid in hard_deleted_ids:
        try:
            await delete_paper_vectors(pid)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Qdrant cleanup failed for paper %d after bulk hard_delete; "
                "vectors are now orphans",
                pid,
            )

    return {"succeeded": succeeded, "failed": failed}


# ---------------------------------------------------------------------------
# POST /api/papers/process_batch
# ---------------------------------------------------------------------------


@router.post("/process_batch", response_model=JobCreateResponse)
@limiter.limit("10/minute")
async def process_batch(
    request: Request,
    body: ProcessBatchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Enqueue a ``papers.batch_process`` job for the given paper IDs.

    Accepts 1–50 explicit paper IDs and immediately queues the job without
    any pre-flight filtering.  The caller can poll progress via
    ``GET /api/jobs/{job_id}``.

    Returns ``{"job_id": "<uuid>", "status": "queued"}``.
    """
    _ = request  # required by @limiter.limit; not used in body
    from jarvis_common.task_registry import KIND_TO_TASK

    user_id = await current_user_id_or_none(request)

    # Assert ownership for each paper before enqueuing to prevent IDOR via
    # batch-processing another user's papers.
    async with db_pool.acquire() as conn:
        for paper_id in body.paper_ids:
            await assert_paper_ownership(conn, paper_id, user_id)

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["papers.batch_process"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_ids=body.paper_ids
    )
    return {"job_id": jarvis_job_id, "status": "queued"}
