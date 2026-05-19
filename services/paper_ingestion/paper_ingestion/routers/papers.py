"""Paper CRUD and metadata endpoints."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, JobCreateResponse, escape_like
from jarvis_common.auth import get_current_user_id
from jarvis_common.library import add_to_library
from jarvis_common.paper_state import (  # noqa: I001
    assert_paper_in_states as _assert_paper_in_states,
    restore_paper as _restore_paper,
    trash_paper as _trash_paper,
    upsert_paper_user_state as _upsert_paper_user_state,
)

from paper_ingestion import papers_service
from paper_ingestion.converters import (
    row_to_chunk_response,
    row_to_paper_response,
    row_to_summary_response,
)
from paper_ingestion.deps import get_db_pool, get_optional_embedder, limiter
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
    caller_id: int = Depends(get_current_user_id),
) -> list[dict]:
    """Return lightweight paper list for selector dropdowns."""
    # Sprint B: scope to the caller's user_library. WS-CROSS-USER: the
    # resolver now hard-401s sessionless callers, so caller_id is always a
    # real user and the previous unscoped-corpus fallback is removed (it
    # leaked every user's papers to API-key-only callers).
    async with db_pool.acquire() as conn:
        if search:
            rows = await conn.fetch(
                """SELECT p.id, p.title, p.source_type, p.published_date
                   FROM papers p
                   JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $2
                   WHERE p.title ILIKE '%' || $1 || '%' ESCAPE '\\'
                   ORDER BY p.created_at DESC
                   LIMIT 200""",
                escape_like(search),
                caller_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT p.id, p.title, p.source_type, p.published_date
                   FROM papers p
                   JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
                   ORDER BY p.created_at DESC
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
    user_id: int = Depends(get_current_user_id),
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
                    q, db_pool, limit=limit, offset=offset, user_id=user_id
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

    if user_id is not None:
        # RB-1: unconditionally scope to the caller's user_library so papers
        # from other tenants are never returned, regardless of which filters
        # are active (view, source_type, topic_id, q, or none at all).
        params.append(user_id)
        joins.append(f"JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = ${len(params)}")

    if view is not None:
        # Bind the user_id so other users' state rows do not leak into the
        # predicate. Mirrors the LEFT JOIN pattern used by routers/feed.py.
        params.append(user_id)
        joins.append(
            "LEFT JOIN paper_user_state pus ON pus.paper_id = p.id"
            f" AND pus.user_id = ${len(params)}"
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
    user_id: int = Depends(get_current_user_id),
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
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
               WHERE paper_id = $1 AND user_id = $2
               LIMIT 1""",
            paper_id,
            user_id,
        )
        feedback_row = await conn.fetchrow(
            """SELECT signal, source, created_at
               FROM recommendation_feedback
               WHERE paper_id = $1 AND user_id = $2
               ORDER BY created_at DESC LIMIT 1""",
            paper_id,
            user_id,
        )
        project_link_count = await conn.fetchval(
            "SELECT COUNT(*) FROM project_papers WHERE paper_id = $1",
            paper_id,
        )
        # Most-recent paper.process / paper.analyze job for this paper+user.
        # Surfaces the SAME persisted failure signal ActionsSidebar polls via
        # getJob (procrastinate_jobs.status='failed') so the left Pipeline rail
        # can show ✗ — no parallel status, no new table.
        last_process_job_status = await conn.fetchval(
            """
            SELECT CASE pj.status
                     WHEN 'failed' THEN 'failed'
                     ELSE 'other' END
            FROM procrastinate_jobs pj
            WHERE pj.task_name IN ('paper.process', 'paper.analyze')
              AND pj.args->>'paper_id' = $1::text
              AND pj.args->>'user_id' = $2::text
            ORDER BY pj.id DESC
            LIMIT 1
            """,
            paper_id,
            user_id,
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
    processing_failed = last_process_job_status == "failed"

    return PaperDetailResponse(
        paper=paper,
        summary=summary,
        chunks=chunks,
        user_state=user_state,
        recent_feedback=recent_feedback,
        has_project_links=has_project_links,
        processing_failed=processing_failed,
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
    user_id: int = Depends(get_current_user_id),
) -> list[PaperResponse]:
    """Upsert a list of papers to the database (by external_id)."""
    max_batch = 100
    if len(papers) > max_batch:
        raise HTTPException(400, f"Batch size cannot exceed {max_batch}")
    if not papers:
        return []
    # Sprint B canonical-corpus: papers are inserted into the canonical
    # corpus (no owner column), then mirrored into the caller's user_library
    # so they show up in *their* feed.
    results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper in papers:
                # Wave 1cd Task B6: stamp citation_batch origin (overrides
                # PaperCreate's "user_initiated" default — the batch endpoint
                # is the canonical citation-graph fan-out path).
                paper.discovery_origin = "citation_batch"
                row = await upsert_paper(conn, paper, discovered_by=user_id)
                if user_id is not None:
                    await add_to_library(
                        conn,
                        user_id=user_id,
                        paper_id=row["id"],
                        added_via="batch_save",
                    )
                results.append(row_to_paper_response(row))
    return results


@router.post("/{paper_id}/feedback", response_model=FeedbackResponse)
@limiter.limit("60/minute")
async def submit_feedback(
    request: Request,
    paper_id: int,
    body: FeedbackRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
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
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)

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
    user_id: int = Depends(get_current_user_id),
) -> None:
    """Delete a recommendation_feedback row for this paper+user+source triple.

    Idempotent — returns 204 regardless of whether a row was deleted.
    ``source`` must be supplied as a query parameter (e.g. ``?source=pulse_thumbs``).
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM recommendation_feedback"
            " WHERE paper_id = $1 AND user_id = $2 AND source = $3",
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
    scope: str = Query(default="library", max_length=16),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
):
    """Return per-bucket paper counts (C3: delegates to papers_service)."""
    _ = request  # required by @limiter.limit; not used in body
    return await papers_service.get_feed_counts(scope, db_pool, user_id)


async def _apply_bulk_action(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    action: str,
    *,
    _hard_deleted_ids: list[int] | None = None,
) -> None:
    """Dispatch a single bulk action (C3: delegates to papers_service)."""
    await papers_service._apply_bulk_action(
        conn, paper_id, user_id, action, _hard_deleted_ids=_hard_deleted_ids, router_logger=logger
    )


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/save  — Reading List
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/save", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def save_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
):
    """Save a paper to the Reading List (``state := 'to_read'``)."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    """Revert a saved paper from the Reading List back to the Inbox (``state := 'inbox'``).

    Requires the paper to be in ``to_read`` state; raises 409 otherwise.
    """
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("to_read",))
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
    user_id: int = Depends(get_current_user_id),
):
    """Skip a paper from the Inbox (``state := 'done'``)."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
):
    """Mark a paper as currently being read (``state := 'reading'``)."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
):
    """Mark a paper as done (``state := 'done'``)."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
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
    was_new_star = False
    project_link_count = 0
    auto_push_on_star = False
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
            """SELECT value FROM user_config
               WHERE key = 'zotero.auto_push_on_star'
                 AND (user_id = $1 OR user_id IS NULL)
               ORDER BY user_id IS NULL
               LIMIT 1""",
            user_id,
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
    user_id: int = Depends(get_current_user_id),
):
    """Set ``starred = FALSE``. Does not change reading state."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
):
    """Move paper to Trash. Atomic: ``state_before_trash := state; state := 'trash'``."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
):
    """Restore a paper from Trash to its prior state."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("trash",))
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
    user_id: int = Depends(get_current_user_id),
):
    """Trash the paper AND record negative feedback (``source='dismiss_combined'``).

    Single transaction. The only combined action in the system per spec §4.4.
    """
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
):
    """Update subjective per-paper annotations (rating 1-5, user_notes, flagged).

    Partial updates: any field left as ``None`` is preserved on conflict.
    Returns the resulting :class:`UserStateResponse` so the frontend can
    refresh its local cache without a follow-up GET.
    """
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
    user_id: int = Depends(get_current_user_id),
):
    """Permanently delete a trashed paper.

    Cascades through FK; Qdrant cleanup is best-effort.

    Order rationale (WS-AH2 NEW-H2 — load-bearing): if SQL ``DELETE`` fails,
    the txn rolls back and Qdrant is untouched (user retries cleanly). If
    SQL succeeds and Qdrant fails, vectors are orphaned (recoverable). The
    reverse order is data-loss-prone — do not collapse the inside-txn
    DELETE and outside-txn Qdrant cleanup into a single try/except.

    C3: business logic lives in ``papers_service.hard_delete_paper``; the
    router passes its own ``logger`` so the orphan-vector ``logger.exception``
    keeps the ``paper_ingestion.routers.papers`` logger name.
    """
    _ = request  # required by @limiter.limit; not used in body
    return await papers_service.hard_delete_paper(paper_id, db_pool, user_id, router_logger=logger)


# ---------------------------------------------------------------------------
# POST /api/papers/bulk
# ---------------------------------------------------------------------------


def _classify_bulk_error(exc: Exception) -> str:
    """Map exceptions to safe, operator-diagnostic response codes.

    Raw exception messages (asyncpg constraint names, SQL text) are never
    forwarded to the caller — only the code string is returned.  The original
    exception is always logged server-side via ``logger.exception`` at the
    call site.
    """
    if isinstance(exc, HTTPException):
        if exc.status_code == 404:
            return "not_found"
        if exc.status_code == 403:
            return "forbidden"
        if exc.status_code == 409:
            return "conflict"
        return "http_error"
    if isinstance(exc, asyncpg.UniqueViolationError):
        return "already_in_state"
    if isinstance(exc, asyncpg.ForeignKeyViolationError):
        return "not_found"
    if isinstance(exc, asyncpg.NotNullViolationError | asyncpg.CheckViolationError):
        return "constraint_error"
    if isinstance(exc, asyncpg.PostgresError):
        return "db_error"
    if isinstance(exc, ValueError):
        return "invalid_action"
    return "unknown_error"


@router.post("/bulk")
@limiter.limit("10/minute")
async def bulk_action_papers(
    request: Request,
    body: BulkActionRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
):
    """Apply a lifecycle action to multiple papers atomically.

    Returns ``{"succeeded": [...], "failed": [{"paper_id": int, "error": str}]}``.
    Partial failures are collected; the outer transaction is committed even when
    individual papers fail (per-paper savepoints isolate rollbacks).
    """
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
                        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
                        await _apply_bulk_action(
                            conn,
                            paper_id,
                            user_id,
                            body.action,
                            _hard_deleted_ids=hard_deleted_ids,
                        )
                    succeeded.append(paper_id)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "bulk_action_papers: paper_id=%d action=%s failed",
                        paper_id,
                        body.action,
                    )
                    failed.append({"paper_id": paper_id, "error": _classify_bulk_error(exc)})

    # Qdrant cleanup runs OUTSIDE the PostgreSQL transaction to avoid deadlock /
    # SAVEPOINT bloat.  Failures are logged but do not affect the HTTP response.
    for pid in hard_deleted_ids:
        try:
            await papers_service.delete_paper_vectors(pid)
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
    user_id: int = Depends(get_current_user_id),
):
    """Enqueue a ``papers.batch_process`` job for the given paper IDs.

    Accepts 1–50 explicit paper IDs and immediately queues the job without
    any pre-flight filtering.  The caller can poll progress via
    ``GET /api/jobs/{job_id}``.

    Returns ``{"job_id": "<uuid>", "status": "queued"}``.
    """
    _ = request  # required by @limiter.limit; not used in body
    from jarvis_common.task_registry import KIND_TO_TASK

    # Assert ownership for each paper before enqueuing to prevent IDOR via
    # batch-processing another user's papers.
    async with db_pool.acquire() as conn:
        for paper_id in body.paper_ids:
            await papers_service.assert_paper_ownership(conn, paper_id, user_id)

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["papers.batch_process"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_ids=body.paper_ids
    )
    return {"job_id": jarvis_job_id, "status": "queued"}
