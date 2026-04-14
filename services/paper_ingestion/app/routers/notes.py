"""Paper Notes CRUD endpoints."""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import delete_or_404, dynamic_update

from app.deps import get_db_pool, limiter
from app.models import NoteCreate, NoteResponse, NoteUpdate

logger = logging.getLogger(__name__)
router = APIRouter(tags=["notes"])

_NOTE_ALLOWED_COLUMNS: set[str] = {"user_note", "highlight_text", "page_number"}


@router.get("/api/papers/{paper_id}/notes", response_model=list[NoteResponse])
@limiter.limit("60/minute")
async def list_notes(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[NoteResponse]:
    """List all notes for a paper, ordered by creation time descending.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.

    Returns
    -------
    list[NoteResponse]
        Notes for the paper.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM paper_notes WHERE paper_id = $1 ORDER BY created_at DESC",
            paper_id,
        )
    return [NoteResponse(**dict(r)) for r in rows]


@router.post(
    "/api/papers/{paper_id}/notes",
    response_model=NoteResponse,
    status_code=201,
)
@limiter.limit("30/minute")
async def create_note(
    request: Request,
    paper_id: int,
    body: NoteCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> NoteResponse:
    """Create a new note for a paper.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.
    body : NoteCreate
        Note content and optional highlight/page info.

    Returns
    -------
    NoteResponse
        The newly created note.
    """
    async with db_pool.acquire() as conn:
        paper = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not paper:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")

        row = await conn.fetchrow(
            """INSERT INTO paper_notes (paper_id, user_note, highlight_text, page_number)
            VALUES ($1, $2, $3, $4) RETURNING *""",
            paper_id,
            body.user_note,
            body.highlight_text,
            body.page_number,
        )
    return NoteResponse(**dict(row))


@router.put("/api/notes/{note_id}", response_model=NoteResponse)
@limiter.limit("30/minute")
async def update_note(
    request: Request,
    note_id: int,
    body: NoteUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> NoteResponse:
    """Update an existing note.

    Parameters
    ----------
    note_id : int
        Database ID of the note to update.
    body : NoteUpdate
        Fields to update.

    Returns
    -------
    NoteResponse
        The updated note.
    """
    updates = body.model_dump(exclude_unset=True, include=_NOTE_ALLOWED_COLUMNS)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    async with db_pool.acquire() as conn:
        row = await dynamic_update(
            conn,
            "paper_notes",
            note_id,
            updates,
            _NOTE_ALLOWED_COLUMNS,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
    return NoteResponse(**dict(row))


@router.delete("/api/notes/{note_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_note(
    request: Request,
    note_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Delete a note.

    Parameters
    ----------
    note_id : int
        Database ID of the note to delete.
    """
    async with db_pool.acquire() as conn:
        await delete_or_404(
            conn,
            "DELETE FROM paper_notes WHERE id = $1",
            note_id,
            detail=f"Note {note_id} not found",
        )
