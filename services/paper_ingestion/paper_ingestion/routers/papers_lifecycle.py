"""Per-paper state-transition endpoints: save/unsave/skip/reading/done/star/unstar/trash/restore."""

import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import ErrorResponse
from jarvis_common.auth import get_current_user_id
from jarvis_common.paper_state import assert_paper_in_states as _assert_paper_in_states
from jarvis_common.paper_state import restore_paper as _restore_paper
from jarvis_common.paper_state import trash_paper as _trash_paper
from jarvis_common.paper_state import upsert_paper_user_state as _upsert_paper_user_state

from paper_ingestion import papers_service
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import (
    AnnotationsRequest,
    MarkReadResponse,
    UserStateResponse,
)
from paper_ingestion.routers._paper_helpers import _upsert_state_and_starred

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
# PUT /api/papers/{paper_id}/save  — Reading List
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/save", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def save_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    """Save a paper to the Reading List (``state := 'to_read'``)."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_states(
            conn, paper_id, user_id, allowed=("inbox", "done", "to_read", "reading")
        )
        await _upsert_state_and_starred(conn, paper_id, user_id, state="to_read")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/unsave  — revert to_read → inbox
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/unsave", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def unsave_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    """Revert a saved paper from the Reading List back to the Inbox (``state := 'inbox'``)."""
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
) -> dict[str, object]:
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
) -> dict[str, object]:
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
) -> dict[str, object]:
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
) -> dict[str, object]:
    """Set ``starred = TRUE``. Does not change reading state.

    Side effect: enqueues a ``zotero.push`` job iff all three conditions hold:
    1. The paper was not already starred (off→on transition).
    2. The paper is linked to at least one project (``project_papers`` row).
    3. ``zotero.auto_push_on_star`` is ``true`` in ``user_config``.
    """
    _ = request  # required by @limiter.limit; not used in body
    was_new_star = False
    project_link_count = 0
    auto_push_on_star = False
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
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
            from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

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
) -> dict[str, object]:
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
) -> dict[str, object]:
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
) -> dict[str, object]:
    """Restore a paper from Trash to its prior state."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("trash",))
        await _restore_paper(conn, paper_id, user_id)
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
) -> UserStateResponse:
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
# DELETE /api/papers/{paper_id}  — hard delete (preserves NEW-H2 ordering)
# ---------------------------------------------------------------------------


@router.delete("/{paper_id}")
@limiter.limit("10/minute")
async def hard_delete_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> dict[str, int]:
    """Permanently delete a trashed paper.

    Cascades through FK; Qdrant cleanup is best-effort.
    C3: business logic lives in ``papers_service.hard_delete_paper``.
    """
    _ = request  # required by @limiter.limit; not used in body
    return await papers_service.hard_delete_paper(paper_id, db_pool, user_id, router_logger=logger)
