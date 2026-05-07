"""Paper Notes CRUD endpoints."""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership, delete_or_404, dynamic_update
from jarvis_common.auth import current_user_id_or_none
from jarvis_common.verify import QuoteVerifier

from paper_ingestion.converters import row_to_chunk_response
from paper_ingestion.deps import get_db_pool, get_verifier, limiter
from paper_ingestion.models import NoteCreate, NoteResponse, NoteUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["notes"])

_NOTE_ALLOWED_COLUMNS: set[str] = {"user_note", "highlight_text", "page_number"}


def _note_response(row: asyncpg.Record | dict) -> NoteResponse:
    """Build a note response with defaults for rows created before migration 037."""
    data = dict(row)
    data.setdefault("verification_status", "unverified")
    data.setdefault("verified_quote", None)
    data.setdefault("verified_page_number", None)
    data.setdefault("promoted_at", None)
    return NoteResponse(**data)


@router.get("/papers/{paper_id}/notes", response_model=list[NoteResponse])
@limiter.limit("60/minute")
async def list_notes(
    request: Request,
    paper_id: int,
    source: str | None = None,
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
    if source not in {None, "user", "zotero"}:
        raise HTTPException(status_code=422, detail="source must be 'user' or 'zotero'")

    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        if source is None:
            rows = await conn.fetch(
                "SELECT * FROM paper_notes WHERE paper_id = $1 ORDER BY created_at DESC",
                paper_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM paper_notes WHERE paper_id = $1 AND source = $2"
                " ORDER BY created_at DESC",
                paper_id,
                source,
            )
    return [_note_response(r) for r in rows]


@router.post(
    "/papers/{paper_id}/notes",
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
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
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
    return _note_response(row)


@router.put("/notes/{note_id}", response_model=NoteResponse)
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

    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        note_source = await conn.fetchval("SELECT source FROM paper_notes WHERE id = $1", note_id)
        if note_source == "zotero":
            raise HTTPException(status_code=403, detail="Zotero annotation notes are read-only")
        # WS-6B-α: ownership check — short-circuits in single-tenant mode.
        if user_id is not None:
            paper_id = await conn.fetchval(
                "SELECT paper_id FROM paper_notes WHERE id = $1", note_id
            )
            if paper_id is not None:
                await assert_paper_ownership(conn, paper_id, user_id)
        row = await dynamic_update(
            conn,
            "paper_notes",
            note_id,
            updates,
            _NOTE_ALLOWED_COLUMNS,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
    return _note_response(row)


@router.post("/notes/{note_id}/promote", response_model=NoteResponse)
@limiter.limit("20/minute")
async def promote_zotero_note(
    request: Request,
    note_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> NoteResponse:
    """Promote a Zotero highlight to verified evidence after quote verification.

    Zotero annotations remain ordinary read-only notes until this explicit
    action verifies their highlight text against ``paper_chunks``. Failed
    verification is recorded on the note but does not set ``promoted_at``.

    COMPLIANCE-001: ``verifier`` is injected from ``app.state.verifier`` (set
    during lifespan startup).  Per-request instantiation of ``QuoteVerifier``
    was wasteful and prevented test injection.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        note = await conn.fetchrow("SELECT * FROM paper_notes WHERE id = $1", note_id)
        if note is None:
            raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
        if note["source"] != "zotero":
            raise HTTPException(
                status_code=400,
                detail="Only Zotero annotation notes can be promoted",
            )
        # WS-6B-α: ownership check (short-circuits when user_id=None).
        await assert_paper_ownership(conn, note["paper_id"], user_id)

        # Idempotency guard: if already verified and promoted, short-circuit —
        # re-running verification would be wasteful and non-deterministic.
        if note["verification_status"] == "verified" and note["promoted_at"] is not None:
            return _note_response(note)

        highlight = str(note["highlight_text"] or "").strip()
        if not highlight:
            raise HTTPException(
                status_code=400,
                detail="Zotero note has no highlight text to verify",
            )

        # WS-4: page-window optimisation — when the annotation carries a page
        # number, first try a narrow ±2-page window to avoid loading the entire
        # paper into memory just for one verification call.  If the window
        # misses (no match found), fall back to the full chunk set.
        note_page = note["page_number"]
        result = None
        if note_page is not None:
            window_rows = await conn.fetch(
                "SELECT * FROM paper_chunks"
                " WHERE paper_id = $1"
                "   AND page_number BETWEEN $2 AND $3"
                " ORDER BY chunk_index",
                note["paper_id"],
                note_page - 2,
                note_page + 2,
            )
            if window_rows:
                window_chunks = [row_to_chunk_response(row) for row in window_rows]
                window_full_text = "\n\n".join(chunk.content for chunk in window_chunks)
                window_result = verifier.verify_quote(highlight, window_full_text, window_chunks)
                if window_result.verified:
                    result = window_result

        if result is None:
            # Either no page_number, window had no rows, or window verification failed —
            # fall back to the full paper chunk set.
            chunk_rows = await conn.fetch(
                "SELECT * FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index",
                note["paper_id"],
            )
            chunks = [row_to_chunk_response(row) for row in chunk_rows]
            full_text = "\n\n".join(chunk.content for chunk in chunks)
            result = verifier.verify_quote(highlight, full_text, chunks)

        if result.verified:
            row = await conn.fetchrow(
                """
                UPDATE paper_notes
                   SET verification_status = 'verified',
                       verified_quote = $1,
                       verified_page_number = $2,
                       promoted_at = NOW()
                 WHERE id = $3
                 RETURNING *
                """,
                result.matched_text or highlight,
                result.page_number or note["page_number"],
                note_id,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE paper_notes
                   SET verification_status = 'failed',
                       verified_quote = NULL,
                       verified_page_number = NULL,
                       promoted_at = NULL
                 WHERE id = $1
                 RETURNING *
                """,
                note_id,
            )

    return _note_response(row)


@router.delete("/notes/{note_id}", status_code=204)
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
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        note_source = await conn.fetchval("SELECT source FROM paper_notes WHERE id = $1", note_id)
        if note_source == "zotero":
            raise HTTPException(status_code=403, detail="Zotero annotation notes are read-only")
        # WS-6B-α: ownership check — short-circuits in single-tenant mode.
        if user_id is not None:
            paper_id = await conn.fetchval(
                "SELECT paper_id FROM paper_notes WHERE id = $1", note_id
            )
            if paper_id is not None:
                await assert_paper_ownership(conn, paper_id, user_id)
        await delete_or_404(
            conn,
            "DELETE FROM paper_notes WHERE id = $1",
            note_id,
            detail=f"Note {note_id} not found",
        )
