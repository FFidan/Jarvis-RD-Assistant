"""Paper Notes CRUD endpoints."""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership, delete_or_404, dynamic_update
from jarvis_common.auth import get_current_user_id
from jarvis_common.verify import QuoteVerifier

from paper_ingestion.converters import row_to_chunk_response
from paper_ingestion.deps import get_db_pool, get_verifier, limiter
from paper_ingestion.models import NoteCreate, NoteResponse, NoteUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["notes"])

_NOTE_ALLOWED_COLUMNS: set[str] = {"user_note", "highlight_text", "page_number"}
_NOTE_RESPONSE_COLUMNS = "pn.*, (pn.content_generation <> p.content_generation) AS stale"


def _note_response(row: asyncpg.Record | dict) -> NoteResponse:
    """Build a note response with defaults for rows created before migration 037."""
    data = dict(row)
    data.setdefault("verification_status", "unverified")
    data.setdefault("verified_quote", None)
    data.setdefault("verified_page_number", None)
    data.setdefault("promoted_at", None)
    data.setdefault("stale", False)
    return NoteResponse(**data)


@router.get("/papers/{paper_id}/notes", response_model=list[NoteResponse])
@limiter.limit("60/minute")
async def list_notes(
    request: Request,
    paper_id: int,
    source: str | None = None,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
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

    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        if source is None:
            rows = await conn.fetch(
                f"SELECT {_NOTE_RESPONSE_COLUMNS} FROM paper_notes pn"
                " JOIN papers p ON p.id = pn.paper_id"
                " WHERE pn.paper_id = $1 AND pn.user_id IS NOT DISTINCT FROM $2"
                " ORDER BY pn.created_at DESC",
                paper_id,
                user_id,
            )
        else:
            rows = await conn.fetch(
                f"SELECT {_NOTE_RESPONSE_COLUMNS} FROM paper_notes pn"
                " JOIN papers p ON p.id = pn.paper_id"
                " WHERE pn.paper_id = $1 AND pn.source = $2"
                " AND pn.user_id IS NOT DISTINCT FROM $3"
                " ORDER BY pn.created_at DESC",
                paper_id,
                source,
                user_id,
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
    user_id: int = Depends(get_current_user_id),
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
        async with conn.transaction():
            await assert_paper_ownership(conn, paper_id, user_id)
            paper = await conn.fetchrow(
                "SELECT id FROM papers WHERE id = $1 FOR UPDATE",
                paper_id,
            )
            if not paper:
                raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")

            # Write user_id so notes are scoped to their author.
            # NULL user_id continues to mean "system-shared" (single-tenant legacy).
            note_id = await conn.fetchval(
                """INSERT INTO paper_notes
                       (paper_id, user_id, user_note, highlight_text, page_number,
                        content_generation)
                SELECT $1, $2, $3, $4, $5, p.content_generation
                  FROM papers p
                 WHERE p.id = $1
                RETURNING id""",
                paper_id,
                user_id,
                body.user_note,
                body.highlight_text,
                body.page_number,
            )
            row = await conn.fetchrow(
                f"SELECT {_NOTE_RESPONSE_COLUMNS} FROM paper_notes pn"
                " JOIN papers p ON p.id = pn.paper_id"
                " WHERE pn.id = $1 AND pn.user_id IS NOT DISTINCT FROM $2",
                note_id,
                user_id,
            )
    return _note_response(row)


@router.put("/notes/{note_id}", response_model=NoteResponse)
@limiter.limit("30/minute")
async def update_note(
    request: Request,
    note_id: int,
    body: NoteUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
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
        # W2b B-NOTES: combine ownership + source check into ONE query to prevent
        # existence/type disclosure before ownership is confirmed.
        if user_id is not None:
            # Multi-tenant: ownership check is combined with source lookup.
            # If no row matches (id + user_id), return 404 — user B cannot learn
            # whether the note exists or is Zotero type before ownership is verified.
            note_row = await conn.fetchrow(
                "SELECT paper_id, source FROM paper_notes WHERE id = $1 AND user_id = $2",
                note_id,
                user_id,
            )
            if note_row is None:
                raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
            if note_row["source"] == "zotero":
                raise HTTPException(status_code=403, detail="Zotero annotation notes are read-only")
            # Assert ownership of the parent paper.
            await assert_paper_ownership(conn, note_row["paper_id"], user_id)
        else:
            # Single-tenant (user_id is None): no IDOR risk; source check safe.
            note_row = await conn.fetchrow(
                "SELECT paper_id, source FROM paper_notes WHERE id = $1", note_id
            )
            if note_row is None:
                raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
            if note_row["source"] == "zotero":
                raise HTTPException(status_code=403, detail="Zotero annotation notes are read-only")
        row = await dynamic_update(
            conn,
            "paper_notes",
            note_id,
            updates,
            _NOTE_ALLOWED_COLUMNS,
            extra_where=("user_id", user_id) if user_id is not None else None,
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
        if user_id is not None:
            row = await conn.fetchrow(
                f"SELECT {_NOTE_RESPONSE_COLUMNS} FROM paper_notes pn"
                " JOIN papers p ON p.id = pn.paper_id"
                " WHERE pn.id = $1 AND pn.user_id = $2",
                note_id,
                user_id,
            )
        else:
            row = await conn.fetchrow(
                f"SELECT {_NOTE_RESPONSE_COLUMNS} FROM paper_notes pn"
                " JOIN papers p ON p.id = pn.paper_id"
                " WHERE pn.id = $1",
                note_id,
            )
    return _note_response(row)


async def _lock_note_for_promotion(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    note_id: int,
    user_id: int | None,
) -> asyncpg.Record | None:
    """Lock a note while preserving the trusted single-user lookup path."""
    if user_id is None:
        return await conn.fetchrow(
            "SELECT * FROM paper_notes WHERE id = $1 FOR UPDATE",
            note_id,
        )
    return await conn.fetchrow(
        "SELECT * FROM paper_notes WHERE id = $1 AND user_id = $2 FOR UPDATE",
        note_id,
        user_id,
    )


@router.post("/notes/{note_id}/promote", response_model=NoteResponse)
@limiter.limit("20/minute")
async def promote_zotero_note(
    request: Request,
    note_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    verifier: QuoteVerifier = Depends(get_verifier),
    user_id: int = Depends(get_current_user_id),
) -> NoteResponse:
    """Promote a Zotero highlight to verified evidence after quote verification.

    Zotero annotations remain ordinary read-only notes until this explicit
    action verifies their highlight text against ``paper_chunks``. Failed
    verification is recorded on the note but does not set ``promoted_at``.

    COMPLIANCE-001: ``verifier`` is injected from ``app.state.verifier`` (set
    during lifespan startup).  Per-request instantiation of ``QuoteVerifier``
    was wasteful and prevented test injection.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Lock the note before its source paper. Review actions use the same
            # dependent-row-first ordering, so no path introduces a reversed
            # card/note-to-paper lock sequence.
            note = await _lock_note_for_promotion(conn, note_id, user_id)
            if note is None:
                raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
            if note["source"] != "zotero":
                raise HTTPException(
                    status_code=400,
                    detail="Only Zotero annotation notes can be promoted",
                )
            await assert_paper_ownership(conn, note["paper_id"], user_id)

            # FOR SHARE conflicts with the source-replacement UPDATE and stays
            # held through verification and the final note mutation.
            paper = await conn.fetchrow(
                "SELECT content_generation FROM papers WHERE id = $1 FOR SHARE",
                note["paper_id"],
            )
            if paper is None:
                raise HTTPException(status_code=404, detail="Paper not found")
            if int(note.get("content_generation", 0)) != int(paper["content_generation"]):
                raise HTTPException(
                    status_code=409,
                    detail="This highlight belongs to an earlier version of the paper.",
                )

            # Staleness is checked before this idempotency return: a prior
            # promotion does not make old evidence current after replacement.
            if note["verification_status"] == "verified" and note["promoted_at"] is not None:
                row = await conn.fetchrow(
                    f"SELECT {_NOTE_RESPONSE_COLUMNS} FROM paper_notes pn"
                    " JOIN papers p ON p.id = pn.paper_id WHERE pn.id = $1",
                    note_id,
                )
                return _note_response(row)

            highlight = str(note["highlight_text"] or "").strip()
            if not highlight:
                raise HTTPException(
                    status_code=400,
                    detail="Zotero note has no highlight text to verify",
                )

            # Page-window optimisation — when the annotation carries a page
            # number, first try a narrow ±2-page window to avoid loading the
            # entire paper into memory for one verification call.
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
                    window_result = verifier.verify_quote(
                        highlight, window_full_text, window_chunks
                    )
                    if window_result.verified:
                        result = window_result

            if result is None:
                chunk_rows = await conn.fetch(
                    "SELECT * FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index",
                    note["paper_id"],
                )
                chunks = [row_to_chunk_response(row) for row in chunk_rows]
                full_text = "\n\n".join(chunk.content for chunk in chunks)
                result = verifier.verify_quote(highlight, full_text, chunks)

            if result.verified:
                await conn.execute(
                    """
                    UPDATE paper_notes
                       SET verification_status = 'verified',
                           verified_quote = $1,
                           verified_page_number = $2,
                           promoted_at = NOW()
                     WHERE id = $3
                    """,
                    result.matched_text or highlight,
                    result.page_number or note["page_number"],
                    note_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE paper_notes
                       SET verification_status = 'failed',
                           verified_quote = NULL,
                           verified_page_number = NULL,
                           promoted_at = NULL
                     WHERE id = $1
                    """,
                    note_id,
                )
            row = await conn.fetchrow(
                f"SELECT {_NOTE_RESPONSE_COLUMNS} FROM paper_notes pn"
                " JOIN papers p ON p.id = pn.paper_id WHERE pn.id = $1",
                note_id,
            )

    return _note_response(row)


@router.delete("/notes/{note_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_note(
    request: Request,
    note_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> None:
    """Delete a note.

    Parameters
    ----------
    note_id : int
        Database ID of the note to delete.
    """
    async with db_pool.acquire() as conn:
        # W2b B-NOTES: combine ownership + source check into ONE query to prevent
        # existence/type disclosure before ownership is confirmed.
        if user_id is not None:
            # Multi-tenant: ownership check is combined with source lookup.
            # If no row matches (id + user_id), return 404 — user B cannot learn
            # whether the note exists or is Zotero type before ownership is verified.
            note_row = await conn.fetchrow(
                "SELECT paper_id, source FROM paper_notes WHERE id = $1 AND user_id = $2",
                note_id,
                user_id,
            )
            if note_row is None:
                raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
            if note_row["source"] == "zotero":
                raise HTTPException(status_code=403, detail="Zotero annotation notes are read-only")
            # Assert ownership of the parent paper.
            await assert_paper_ownership(conn, note_row["paper_id"], user_id)
        else:
            # Single-tenant (user_id is None): no IDOR risk; source check safe.
            note_row = await conn.fetchrow(
                "SELECT paper_id, source FROM paper_notes WHERE id = $1", note_id
            )
            if note_row is None:
                raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
            if note_row["source"] == "zotero":
                raise HTTPException(status_code=403, detail="Zotero annotation notes are read-only")
        if user_id is not None:
            await delete_or_404(
                conn,
                "DELETE FROM paper_notes WHERE id = $1 AND user_id = $2",
                note_id,
                user_id,
                detail=f"Note {note_id} not found",
            )
        else:
            await delete_or_404(
                conn,
                "DELETE FROM paper_notes WHERE id = $1",
                note_id,
                detail=f"Note {note_id} not found",
            )
