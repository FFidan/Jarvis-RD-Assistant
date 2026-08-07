"""Per-user spatial PDF highlight CRUD endpoints."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import delete_or_404
from jarvis_common.auth import get_current_user_id

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import HighlightCreate, HighlightResponse, HighlightUpdate
from paper_ingestion.routers.pdf_files import assert_paper_pdf_visible

router = APIRouter(prefix="/api", tags=["highlights"])

_HIGHLIGHT_STALE_RETURNING = (
    " RETURNING paper_highlights.*, "
    "paper_highlights.content_generation <> "
    "(SELECT p.content_generation FROM papers p "
    "WHERE p.id = paper_highlights.paper_id) AS stale"
)


@router.get("/papers/{paper_id}/highlights", response_model=list[HighlightResponse])
@limiter.limit("60/minute")
async def list_highlights(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> list[HighlightResponse]:
    """List the caller's highlights for a paper, ordered by page then id.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.
    """
    async with db_pool.acquire() as conn:
        await assert_paper_pdf_visible(conn, paper_id, user_id)
        rows = await conn.fetch(
            """SELECT h.*, h.content_generation <> p.content_generation AS stale
                 FROM paper_highlights h
                 JOIN papers p ON p.id = h.paper_id
                WHERE h.paper_id = $1 AND h.user_id = $2
                ORDER BY h.page, h.id""",
            paper_id,
            user_id,
        )
    return [HighlightResponse(**dict(r)) for r in rows]


@router.post(
    "/papers/{paper_id}/highlights",
    response_model=HighlightResponse,
    status_code=201,
)
@limiter.limit("30/minute")
async def create_highlight(
    request: Request,
    paper_id: int,
    body: HighlightCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> HighlightResponse:
    """Create a highlight on a paper the caller is allowed to view.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.
    body : HighlightCreate
        Page, geometry, and optional note/color/quote.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await assert_paper_pdf_visible(
                conn,
                paper_id,
                user_id,
                lock_for_update=True,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO paper_highlights (
                    paper_id, user_id, page, rect, note, color, quote, content_generation
                )
                SELECT $1, $2, $3, $4, $5, $6, $7, p.content_generation
                  FROM papers p
                 WHERE p.id = $1
                RETURNING paper_highlights.*, false AS stale
                """,
                paper_id,
                user_id,
                body.page,
                body.rect.model_dump(),
                body.note,
                body.color,
                body.quote,
            )
            if row is None:
                raise RuntimeError("highlight insert RETURNING always yields a row")
    return HighlightResponse(**dict(row))


@router.patch("/highlights/{highlight_id}", response_model=HighlightResponse)
@limiter.limit("30/minute")
async def update_highlight(
    request: Request,
    highlight_id: int,
    body: HighlightUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> HighlightResponse:
    """Update a highlight's note and/or color (owner only).

    Scoping the UPDATE by ``user_id`` makes the ownership check atomic: a row
    owned by another user matches nothing and yields an opaque 404.

    Parameters
    ----------
    highlight_id : int
        Database ID of the highlight to update.
    body : HighlightUpdate
        Fields to update (note and/or color).
    """
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    note_set = "note" in fields
    color_set = "color" in fields
    async with db_pool.acquire() as conn:
        if note_set and color_set:
            row = await conn.fetchrow(
                "UPDATE paper_highlights SET note = $1, color = $2"
                " WHERE id = $3 AND user_id = $4" + _HIGHLIGHT_STALE_RETURNING,
                fields["note"],
                fields["color"],
                highlight_id,
                user_id,
            )
        elif note_set:
            row = await conn.fetchrow(
                "UPDATE paper_highlights SET note = $1 WHERE id = $2 AND user_id = $3"
                + _HIGHLIGHT_STALE_RETURNING,
                fields["note"],
                highlight_id,
                user_id,
            )
        else:
            row = await conn.fetchrow(
                "UPDATE paper_highlights SET color = $1 WHERE id = $2 AND user_id = $3"
                + _HIGHLIGHT_STALE_RETURNING,
                fields["color"],
                highlight_id,
                user_id,
            )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Highlight {highlight_id} not found")
    return HighlightResponse(**dict(row))


@router.delete("/highlights/{highlight_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_highlight(
    request: Request,
    highlight_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> None:
    """Delete a highlight (owner only).

    Parameters
    ----------
    highlight_id : int
        Database ID of the highlight to delete.
    """
    async with db_pool.acquire() as conn:
        await delete_or_404(
            conn,
            "DELETE FROM paper_highlights WHERE id = $1 AND user_id = $2",
            highlight_id,
            user_id,
            detail=f"Highlight {highlight_id} not found",
        )
