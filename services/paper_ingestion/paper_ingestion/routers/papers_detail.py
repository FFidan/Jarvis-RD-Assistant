"""Paper detail and batch-save endpoints: get_paper_detail, batch_save_papers."""

import logging
import re
import uuid
from typing import Annotated
from urllib.parse import urlparse

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response
from jarvis_common import ErrorResponse
from jarvis_common.auth import get_current_user_id, get_current_user_id_or_bot
from jarvis_common.library import add_to_library, is_in_library
from jarvis_common.paper_state import upsert_paper_user_state
from jarvis_common.paper_visibility import PUBLIC_VISIBILITY_SCOPE

from paper_ingestion import papers_service
from paper_ingestion.citation_format import (
    CitationBulkRequest,
    CitationFormat,
    build_citations,
    content_type,
    file_extension,
)
from paper_ingestion.citations import _filter_visible_paper_ids
from paper_ingestion.converters import (
    filter_current_cross_references,
    row_to_chunk_response,
    row_to_paper_response,
    row_to_summary_response,
)
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.integrations.zotero_service import _resolve_zotero_user_id
from paper_ingestion.models import (
    PaperCreate,
    PaperDetailResponse,
    PaperResponse,
    RecentFeedback,
    SourceType,
    UserStateResponse,
)
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from paper_ingestion.services.markdown_export import build_paper_markdown
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

# external_ids in these namespaces are minted server-side by first-party
# ingestion (local PDF uploads, Zotero sync). A client batch-save entry claiming
# one could squat a row that a later genuine ingest would trust as its own.
_RESERVED_EXTERNAL_ID_PREFIXES = (f"{SourceType.LOCAL.value}:", f"{SourceType.ZOTERO.value}:")


# ---------------------------------------------------------------------------
# GET /api/papers/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/{paper_id}", response_model=PaperDetailResponse)
@limiter.limit("60/minute")
async def get_paper_detail(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id_or_bot),
) -> PaperDetailResponse:
    """Get a paper with its summary, chunks, user state, and most recent feedback."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        paper_row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
        if not paper_row:
            raise HTTPException(status_code=404, detail="Paper not found")

        # paper_summaries is per-user workspace content (UNIQUE NULLS NOT
        # DISTINCT (paper_id, user_id)); scope the read to the caller so a
        # shared canonical paper never serves another user's summary. IS NOT
        # DISTINCT FROM keeps single-user mode (user_id IS NULL) matching.
        summary_row = await conn.fetchrow(
            "SELECT * FROM paper_summaries WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2 "
            "AND content_generation = (SELECT content_generation FROM papers WHERE id = $1)",
            paper_id,
            user_id,
        )
        current_cross_references = (
            await filter_current_cross_references(conn, list(summary_row["cross_references"] or []))
            if summary_row
            else []
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
        # Scope the link count to the caller's own projects. Without
        # the projects JOIN this counted other users' links too, leaking a
        # cross-tenant signal (gates the "Send to Zotero" button). NULL-safe
        # predicate keeps single-user mode (user_id IS NULL) counting all rows.
        project_link_count = await conn.fetchval(
            """SELECT COUNT(*)
               FROM project_papers pp
               JOIN projects pr ON pr.id = pp.project_id
                 AND ($2::bigint IS NULL OR pr.user_id IS NOT DISTINCT FROM $2)
               WHERE pp.paper_id = $1""",
            paper_id,
            user_id,
        )
        last_process_job_status = await conn.fetchval(
            """
            SELECT CASE pj.status
                     WHEN 'failed' THEN 'failed'
                     ELSE 'other' END
            FROM procrastinate_jobs pj
            WHERE pj.task_name IN ('paper.process', 'paper.analyze')
              AND pj.args->>'paper_id' = $1::text
              AND ($2::text IS NULL OR pj.args->>'user_id' = $2::text)
            ORDER BY pj.id DESC
            LIMIT 1
            """,
            str(paper_id),
            str(user_id) if user_id is not None else None,
        )

    paper = row_to_paper_response(paper_row)
    summary = (
        row_to_summary_response(summary_row, cross_references=current_cross_references)
        if summary_row
        else None
    )
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
# Citation export (BibTeX / RIS)
# ---------------------------------------------------------------------------


def _safe_filename(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", stem)[:100]


def _citation_response(text: str, fmt: CitationFormat, stem: str) -> Response:
    filename = f"{_safe_filename(stem)}.{file_extension(fmt)}"
    return Response(
        content=text,
        media_type=content_type(fmt),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{paper_id}/citation")
@limiter.limit("60/minute")
async def get_paper_citation(
    request: Request,
    paper_id: int,
    format: CitationFormat = CitationFormat.BIBTEX,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> Response:
    """Return a single paper's citation as BibTeX or RIS text (file-download)."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        resolved_uid = await _resolve_zotero_user_id(conn, user_id)
        row = await conn.fetchrow(
            """
            SELECT p.*, l.zotero_citation_key AS link_citation_key
            FROM papers p
            LEFT JOIN paper_user_zotero_links l
                ON l.paper_id = p.id AND l.user_id = $2
            WHERE p.id = $1
            """,
            paper_id,
            resolved_uid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")

    paper = dict(row) | {"zotero_citation_key": row["link_citation_key"]}
    stem = row["link_citation_key"] or f"paper-{paper_id}"
    text = build_citations([paper], format)
    return _citation_response(text, format, stem)


@router.post("/citations")
@limiter.limit("60/minute")
async def get_papers_citations(
    request: Request,
    body: CitationBulkRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> Response:
    """Return concatenated citations for the caller-visible subset of paper_ids.

    Input order is preserved; ids the caller cannot see are silently dropped.
    """
    async with db_pool.acquire() as conn:
        visible_ids = await _filter_visible_paper_ids(conn, body.paper_ids, user_id)
        if not visible_ids:
            raise HTTPException(status_code=404, detail="No citable papers found")
        resolved_uid = await _resolve_zotero_user_id(conn, user_id)
        rows = await conn.fetch(
            """
            SELECT p.*, l.zotero_citation_key AS link_citation_key
            FROM papers p
            LEFT JOIN paper_user_zotero_links l
                ON l.paper_id = p.id AND l.user_id = $2
            WHERE p.id = ANY($1::int[])
            """,
            visible_ids,
            resolved_uid,
        )

    rows_by_id = {
        row["id"]: dict(row) | {"zotero_citation_key": row["link_citation_key"]} for row in rows
    }
    ordered = [rows_by_id[pid] for pid in body.paper_ids if pid in rows_by_id]
    text = build_citations(ordered, body.format)
    return _citation_response(text, body.format, "citations")


# ---------------------------------------------------------------------------
# Markdown knowledge export
# ---------------------------------------------------------------------------


@router.get("/{paper_id}/export.md")
@limiter.limit("30/minute")
async def export_paper_markdown(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> Response:
    """Return the paper's summaries, notes, cards, and extractions as Markdown."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        export = await build_paper_markdown(conn, paper_id, user_id)

    filename = f"{_safe_filename(export.stem)}.md"
    return Response(
        content=export.text,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    """Upsert a list of papers to the database (by external_id).

    pdf_url is validated against ALLOWED_PDF_DOMAINS at persistence time;
    non-allowlisted URLs are cleared before upsert.
    """
    max_batch = 100
    if len(papers) > max_batch:
        raise HTTPException(400, f"Batch size cannot exceed {max_batch}")
    if not papers:
        return []
    results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper in papers:
                if paper.external_id.startswith(_RESERVED_EXTERNAL_ID_PREFIXES):
                    raise HTTPException(400, "external_id uses a reserved namespace")
                # Validate pdf_url against ALLOWED_PDF_DOMAINS; clear non-allowlisted URLs.
                if paper.pdf_url is not None:
                    try:
                        parsed = urlparse(str(paper.pdf_url))
                        hostname = parsed.hostname or ""
                        if (
                            parsed.scheme not in ("http", "https")
                            or hostname not in ALLOWED_PDF_DOMAINS
                        ):
                            paper.pdf_url = None  # type: ignore[assignment]
                    except Exception:
                        paper.pdf_url = None  # type: ignore[assignment]
                # Stamp citation_batch origin
                paper.discovery_origin = "citation_batch"
                row = await upsert_paper(conn, paper, discovered_by=user_id)
                if user_id is not None:
                    # Attach + echo only when the caller has a legitimate claim
                    # on the canonical row. A colliding external_id returns
                    # another tenant's existing private row unchanged (attach-only
                    # upsert); attaching it would grant that user's raw-PDF access
                    # and leak the private metadata through the echoed response,
                    # so such collisions are skipped (no attach, no echo, and
                    # therefore no analyze-enqueue below).
                    already_in_library = await is_in_library(
                        conn, user_id=user_id, paper_id=row["id"]
                    )
                    attachable = (
                        row["is_insert"]
                        or row["visibility_scope"] == PUBLIC_VISIBILITY_SCOPE
                        or already_in_library
                    )
                    if not attachable:
                        continue
                    if not already_in_library:
                        await add_to_library(
                            conn,
                            user_id=user_id,
                            paper_id=row["id"],
                            added_via="batch_save",
                        )
                        await upsert_paper_user_state(
                            conn,
                            row["id"],
                            user_id,
                            state="to_read",
                        )
                results.append(row_to_paper_response(row))
    if not results:
        return results
    # A payload may name the same external_id more than once; duplicates
    # resolve to one canonical row, which must be analyzed only once.
    saved_ids = list(dict.fromkeys(saved.id for saved in results))
    async with db_pool.acquire() as conn:
        analysis_ids = await papers_service.find_papers_needing_analysis(conn, saved_ids)
    for paper_id in saved_ids:
        if paper_id not in analysis_ids:
            continue
        try:
            from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

            await KIND_TO_TASK["paper.analyze"].defer_async(
                job_id=str(uuid.uuid4()), user_id=user_id, paper_id=paper_id
            )
        except Exception:
            logger.exception("paper.analyze enqueue failed for paper %d", paper_id)
    return results
