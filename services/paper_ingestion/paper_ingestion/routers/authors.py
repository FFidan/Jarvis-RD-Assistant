"""Author tracking and alert endpoints.

Provides CRUD for tracked authors, auto-detection from starred/rated papers,
and a check endpoint that matches tracked authors against recent papers.
"""

import logging
from datetime import UTC, datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import author_matches, delete_or_404, dynamic_update, log_audit
from jarvis_common.auth import current_user_id_strict

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import (
    AuthorCheckResponse,
    AutoDetectResponse,
    TrackedAuthorCreate,
    TrackedAuthorResponse,
    TrackedAuthorUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/authors", tags=["authors"])

_AUTHOR_ALLOWED_COLUMNS: set[str] = {"enabled", "s2_author_id"}


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TrackedAuthorResponse])
@limiter.limit("60/minute")
async def list_tracked_authors(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[TrackedAuthorResponse]:
    """List all tracked authors for the current user."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tracked_authors"
            " WHERE user_id IS NOT DISTINCT FROM $1 ORDER BY author_name",
            user_id,
        )
    return [TrackedAuthorResponse(**dict(r)) for r in rows]


@router.post("", response_model=TrackedAuthorResponse, status_code=201)
@limiter.limit("30/minute")
async def create_tracked_author(
    request: Request,
    body: TrackedAuthorCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> TrackedAuthorResponse:
    """Add a new tracked author for the current user."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        # Check for duplicates scoped to this user
        existing = await conn.fetchrow(
            """SELECT id FROM tracked_authors
            WHERE author_name = $1 AND s2_author_id IS NOT DISTINCT FROM $2
              AND user_id IS NOT DISTINCT FROM $3""",
            body.author_name,
            body.s2_author_id,
            user_id,
        )
        if existing:
            raise HTTPException(status_code=409, detail="Author already tracked")

        row = await conn.fetchrow(
            """INSERT INTO tracked_authors (author_name, s2_author_id, source, user_id)
            VALUES ($1, $2, 'manual', $3) RETURNING *""",
            body.author_name,
            body.s2_author_id,
            user_id,
        )
    return TrackedAuthorResponse(**dict(row))


@router.put("/{author_id}", response_model=TrackedAuthorResponse)
@limiter.limit("30/minute")
async def update_tracked_author(
    request: Request,
    author_id: int,
    body: TrackedAuthorUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> TrackedAuthorResponse:
    """Update a tracked author (enable/disable, change S2 ID)."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM tracked_authors WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2",
            author_id,
            user_id,
        )
        if not existing:
            raise HTTPException(404, f"Tracked author {author_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_AUTHOR_ALLOWED_COLUMNS)
        if not updates:
            return TrackedAuthorResponse(**dict(existing))

        row = await dynamic_update(
            conn,
            "tracked_authors",
            author_id,
            updates,
            _AUTHOR_ALLOWED_COLUMNS,
        )
    return TrackedAuthorResponse(**dict(row))


@router.delete("/{author_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_tracked_author(
    request: Request,
    author_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Remove a tracked author."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        await delete_or_404(
            conn,
            "DELETE FROM tracked_authors WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2",
            author_id,
            user_id,
            detail=f"Tracked author {author_id} not found",
        )
    await log_audit(
        db_pool,
        action="delete_tracked_author",
        resource=f"author:{author_id}",
    )


# ---------------------------------------------------------------------------
# Auto-detect and check endpoints
# ---------------------------------------------------------------------------


@router.post("/auto-detect", response_model=AutoDetectResponse)
@limiter.limit("10/minute")
async def auto_detect_authors(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> AutoDetectResponse:
    """Auto-detect authors from starred or highly-rated papers.

    Scans papers that are starred or have rating >= 4 and adds their
    authors to the tracked_authors table with source ``auto_starred``
    or ``auto_rated``.
    """
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT author_name,
                      bool_or(COALESCE(starred, FALSE)) AS is_starred,
                      max(rating) AS max_rating
            FROM (
                SELECT unnest(p.authors) AS author_name, pus.starred, pus.rating
                FROM papers p
                JOIN paper_user_state pus ON p.id = pus.paper_id
                WHERE (pus.starred = TRUE OR pus.rating >= 4)
                  AND pus.user_id IS NOT DISTINCT FROM $1
            ) sub
            GROUP BY author_name""",
            user_id,
        )

        added = 0
        already_tracked = 0
        new_authors: list[TrackedAuthorResponse] = []

        async with conn.transaction():
            for row in rows:
                author_name = row["author_name"]
                source = "auto_starred" if row["is_starred"] else "auto_rated"

                existing = await conn.fetchrow(
                    "SELECT id FROM tracked_authors "
                    "WHERE author_name = $1 AND s2_author_id IS NULL "
                    "  AND user_id IS NOT DISTINCT FROM $2",
                    author_name,
                    user_id,
                )
                if existing:
                    already_tracked += 1
                    continue

                try:
                    new_row = await conn.fetchrow(
                        """INSERT INTO tracked_authors (author_name, source, user_id)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (user_id, author_name, s2_author_id) DO NOTHING
                        RETURNING *""",
                        author_name,
                        source,
                        user_id,
                    )
                    if new_row:
                        added += 1
                        new_authors.append(TrackedAuthorResponse(**dict(new_row)))
                    else:
                        already_tracked += 1
                except Exception:
                    logger.exception("Failed to insert auto-detected author %s", author_name)
                    already_tracked += 1

    return AutoDetectResponse(
        added=added,
        already_tracked=already_tracked,
        authors=new_authors,
    )


@router.post("/check", response_model=AuthorCheckResponse)
@limiter.limit("30/minute")
async def check_tracked_authors(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> AuthorCheckResponse:
    """Check tracked authors against recent papers (last 24 hours).

    Matches papers by S2 author ID (if available) or normalized name.
    Logs matches in author_alert_log for deduplication.
    """
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        authors = await conn.fetch(
            "SELECT * FROM tracked_authors"
            " WHERE enabled = TRUE AND user_id IS NOT DISTINCT FROM $1",
            user_id,
        )
        if not authors:
            return AuthorCheckResponse(new_papers=0, authors_checked=0)

        # Fetch papers from the last 24 hours scoped to the caller's library
        recent_papers = await conn.fetch(
            """SELECT p.id, p.authors, p.metadata
            FROM papers p
            JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
            WHERE p.created_at >= NOW() - INTERVAL '24 hours'""",
            user_id,
        )

        total_new = 0

        async with conn.transaction():
            for author_row in authors:
                author_id = author_row["id"]
                tracked_name = author_row["author_name"]
                s2_id = author_row["s2_author_id"]

                for paper in recent_papers:
                    paper_id = paper["id"]
                    paper_authors = paper["authors"] or []
                    paper_metadata = paper["metadata"] or {}

                    matched = False

                    # Precise match: S2 author ID
                    if s2_id:
                        s2_author_ids = [
                            str(entry["authorId"])
                            for entry in paper_metadata.get("s2_author_ids", [])
                            if isinstance(entry, dict) and entry.get("authorId")
                        ]
                        if s2_id in s2_author_ids:
                            matched = True

                    # Fallback: name matching
                    if not matched:
                        for candidate in paper_authors:
                            if author_matches(tracked_name, candidate):
                                matched = True
                                break

                    if matched:
                        # Deduplicate via author_alert_log (per-user since migration 0091)
                        row = await conn.fetchrow(
                            """INSERT INTO author_alert_log (tracked_author_id, paper_id, user_id)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (tracked_author_id, paper_id, user_id) DO NOTHING
                            RETURNING tracked_author_id""",
                            author_id,
                            paper_id,
                            user_id,
                        )
                        if row:
                            total_new += 1

                # Update last_checked_at
                await conn.execute(
                    "UPDATE tracked_authors SET last_checked_at = $1"
                    " WHERE id = $2 AND user_id IS NOT DISTINCT FROM $3",
                    datetime.now(UTC),
                    author_id,
                    user_id,
                )

    return AuthorCheckResponse(new_papers=total_new, authors_checked=len(authors))
