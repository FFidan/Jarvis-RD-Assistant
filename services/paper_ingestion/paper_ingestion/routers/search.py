"""Search and relevance-scoring endpoints.

This module owns:

* ``POST /api/search``               — multi-source search + DB upsert
* ``POST /api/search-preview``       — multi-source search without DB writes
* ``POST /api/papers/search-hybrid`` — BM25 + semantic RRF hybrid search
* ``POST /api/relevance-score``      — paper/topic relevance score

The feed endpoint moved to ``routers/feed.py``; the discovery endpoints
(``/discover`` and ``/similar/{id}``) moved to ``routers/discovery.py``;
shared helpers and response models live in ``routers/search_helpers.py``.

Endpoint-specific orchestration remains here; reusable search behavior lives in
``search_helpers`` and source construction lives in ``source_helper``.
"""

import asyncio
import logging
from datetime import date
from typing import Any

import asyncpg
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common.auth import get_current_user_id
from jarvis_common.db_helpers import assert_paper_ownership
from jarvis_common.library import add_to_library

from paper_ingestion.converters import row_to_paper_response
from paper_ingestion.deps import get_db_pool, get_embedder, get_http_client, limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.job_errors import classify_bulk_error
from paper_ingestion.models import (
    HybridSearchResult,
    PaperCreate,
    PaperResponse,
    RelevanceScoreResponse,
    SearchRequest,
    SourceType,
)
from paper_ingestion.routers import search_helpers
from paper_ingestion.routers.search_helpers import (
    PREVIEW_SOURCE_BOOTSTRAP_EXCEPTIONS,
    MultiSourceSearchResponse,
    SearchPersistenceFailure,
    SearchPreviewResponse,
    SearchPreviewResult,
    SearchPreviewSourceError,
)
from paper_ingestion.services import source_helper
from paper_ingestion.services.pdf_workflow import (
    reclaim_discarded_paper_content,
    upsert_verified_public_paper,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])


async def _resolve_sources_for_search(
    source_types: list[SourceType],
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    request: Request,
) -> tuple[dict[SourceType, Any], dict[SourceType, Exception]]:
    """Resolve source plugins and isolate supported bootstrap failures by source."""
    return await source_helper.get_sources_for_types(
        source_types, db_pool, http_client, request=request
    )


# ---------------------------------------------------------------------------
# POST /api/search
# ---------------------------------------------------------------------------


def _collect_available_plugins(
    source_types: list[SourceType],
    budgets: list[int],
    resolved_plugins: dict[SourceType, Any],
    source_load_errors: dict[SourceType, Exception],
) -> tuple[list[tuple[str, Any, int]], list[str]]:
    """Partition resolved sources into usable plugins and degraded source names."""
    plugins: list[tuple[str, Any, int]] = []
    degraded_sources: list[str] = []
    for st, budget in zip(source_types, budgets):
        plugin = resolved_plugins.get(st)
        if plugin is not None:
            plugins.append((st.value, plugin, budget))
            continue
        exc = source_load_errors.get(st)
        if isinstance(exc, HTTPException):
            logger.warning("Source %s unavailable for search: %s", st.value, exc.detail)
            degraded_sources.append(st.value)
        elif exc is not None:
            logger.warning("Source %s unavailable for search: %s", st.value, exc)
            degraded_sources.append(st.value)
    return plugins, degraded_sources


async def _search_source_papers(
    source_name: str, plugin: Any, budget: int, body: SearchRequest
) -> tuple[str, list[PaperCreate], bool]:
    """Search one source, returning an empty result with a failure flag on error."""
    try:
        papers = await plugin.search(
            body.query,
            budget,
            year_from=body.year_from,
            year_to=body.year_to,
            sort_by=body.sort_by,
            author=body.author,
        )
        return source_name, papers, False
    except Exception as exc:  # broad: heterogeneous plugins raise different exception types
        logger.warning("Source %s search failed: %s", source_name, exc, exc_info=True)
        return source_name, [], True


def _merge_search_results(
    per_source: dict[str, list[PaperCreate]], sort_by: str | None
) -> list[PaperCreate]:
    """Merge per-source results (date sort or round-robin interleave) and dedup."""
    if sort_by == "date":
        all_papers: list[PaperCreate] = []
        for papers in per_source.values():
            all_papers.extend(papers)
        deduped = search_helpers._dedup_papers(all_papers)
        deduped.sort(key=lambda p: p.published_date or date.min, reverse=True)
    else:
        interleaved = search_helpers._round_robin_merge(per_source)
        deduped = search_helpers._dedup_papers(interleaved)
    return deduped


async def _persist_search_results(
    db_pool: asyncpg.Pool, deduped: list[PaperCreate], user_id: int
) -> tuple[list[PaperResponse], list[SearchPersistenceFailure]]:
    """Upsert search results into the DB and add them to the caller's library.

    Each paper is independent: a failed save is reported with a safe error code
    and does not hide earlier work or prevent later results from being saved.
    Storage for content a promotion discarded is reclaimed after the connection
    is released, so this request's connection is not held across the Qdrant and
    filesystem work that reclamation does on a connection of its own.
    """
    saved_results: list[PaperResponse] = []
    failed_results: list[SearchPersistenceFailure] = []
    discarded_content_ids: list[int] = []
    async with db_pool.acquire() as conn:
        for paper in deduped:
            try:
                paper.discovery_origin = "user_initiated"
                row = await upsert_verified_public_paper(
                    conn,
                    paper,
                    discovered_by=user_id,
                    discarded_content_ids=discarded_content_ids,
                )
                if user_id is not None:
                    await add_to_library(
                        conn,
                        user_id=user_id,
                        paper_id=row["id"],
                        added_via="manual_save",
                    )
                saved_results.append(row_to_paper_response(row))
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Search persistence failed for external_id=%s source_type=%s",
                    paper.external_id,
                    paper.source_type.value,
                )
                failed_results.append(
                    SearchPersistenceFailure(
                        external_id=paper.external_id,
                        error=classify_bulk_error(exc),
                    )
                )
    for paper_id in discarded_content_ids:
        await reclaim_discarded_paper_content(paper_id, db_pool)
    return saved_results, failed_results


def _count_results_by_source(
    per_source: dict[str, list[PaperCreate]], deduped: list[PaperCreate]
) -> dict[str, int]:
    """Count deduped results per source name."""
    per_source_counts: dict[str, int] = {}
    for source_name in per_source:
        per_source_counts[source_name] = sum(
            1 for p in deduped if p.source_type.value == source_name
        )
    return per_source_counts


@router.post("/search", response_model=MultiSourceSearchResponse)
@limiter.limit("30/minute")
async def search_papers(
    request: Request,
    body: SearchRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    user_id: int = Depends(get_current_user_id),
) -> MultiSourceSearchResponse:
    """Search for papers across one or more sources and upsert results into the database.

    Parameters
    ----------
    body : SearchRequest
        Query string, source_types list, and max_results.

    Returns
    -------
    MultiSourceSearchResponse
        Papers found, per-source counts, and any degraded sources.
        Papers are deduplicated across sources.
    """
    if not body.source_types:
        raise HTTPException(status_code=400, detail="At least one source must be selected")
    source_types = body.source_types
    n = len(source_types)
    base_per_source = max(1, body.max_results // n)
    budgets = [base_per_source] * n
    remainder = body.max_results - base_per_source * n
    for i in range(remainder):
        budgets[i] += 1

    # Resolve source instances (skip disabled/unknown without failing the whole request)
    resolved_plugins, source_load_errors = await _resolve_sources_for_search(
        source_types, db_pool, http_client, request
    )
    plugins, degraded_sources = _collect_available_plugins(
        source_types, budgets, resolved_plugins, source_load_errors
    )

    # Fan-out search across all available sources concurrently
    raw_results = await asyncio.gather(
        *[_search_source_papers(name, plugin, budget, body) for name, plugin, budget in plugins]
    )

    # Collect per-source results, track errors
    per_source: dict[str, list[PaperCreate]] = {}
    for source_name, papers, failed in raw_results:
        per_source[source_name] = papers
        if failed and source_name not in degraded_sources:
            degraded_sources.append(source_name)

    deduped = _merge_search_results(per_source, body.sort_by)

    # Upsert into DB (per original /api/search behavior).
    # Insert canonical, then add to the caller's user_library so the
    # manually-searched paper appears in *their* feed.
    saved_results, failed_results = await _persist_search_results(db_pool, deduped, user_id)

    per_source_counts = _count_results_by_source(per_source, deduped)

    return MultiSourceSearchResponse(
        results=deduped,
        total=len(deduped),
        per_source_counts=per_source_counts,
        degraded_sources=degraded_sources,
        saved=saved_results,
        failed=failed_results,
    )


def _collect_preview_available_plugins(
    source_types: list[SourceType],
    budgets: list[int],
    resolved_plugins: dict[SourceType, Any],
    source_load_errors: dict[SourceType, Exception],
) -> tuple[list[tuple[str, Any, int]], list[str], dict[str, SearchPreviewSourceError]]:
    """Partition resolved preview sources into usable plugins, degraded names, and errors."""
    plugins: list[tuple[str, Any, int]] = []
    degraded_sources: list[str] = []
    source_errors: dict[str, SearchPreviewSourceError] = {}
    for st, budget in zip(source_types, budgets):
        plugin = resolved_plugins.get(st)
        if plugin is not None:
            plugins.append((st.value, plugin, budget))
            continue
        exc = source_load_errors.get(st)
        if isinstance(exc, HTTPException):
            logger.warning("Source %s unavailable for preview search: %s", st.value, exc.detail)
            source_errors[st.value] = search_helpers._build_preview_source_error(
                st.value, exc, unavailable=True
            )
            degraded_sources.append(st.value)
        elif isinstance(exc, PREVIEW_SOURCE_BOOTSTRAP_EXCEPTIONS):
            logger.warning("Source %s unavailable for preview search: %s", st.value, exc)
            source_errors[st.value] = search_helpers._build_preview_source_error(
                st.value, exc, unavailable=True
            )
            degraded_sources.append(st.value)
        elif exc is not None:
            logger.warning("Source %s unavailable for preview search: %s", st.value, exc)
            source_errors[st.value] = search_helpers._build_preview_source_error(
                st.value, exc, unavailable=True
            )
            degraded_sources.append(st.value)
    return plugins, degraded_sources, source_errors


async def _search_preview_source_papers(
    source_name: str, plugin: Any, budget: int, body: SearchRequest
) -> tuple[str, list[PaperCreate], SearchPreviewSourceError | None]:
    """Search one source for the preview flow, capturing errors instead of raising."""
    try:
        papers = await plugin.search(
            body.query,
            budget,
            year_from=body.year_from,
            year_to=body.year_to,
            sort_by=body.sort_by,
            author=body.author,
        )
        return source_name, papers, None
    except Exception as exc:  # broad: heterogeneous plugins raise different exception types
        logger.warning("Source %s preview search failed: %s", source_name, exc, exc_info=True)
        error = search_helpers._build_preview_source_error(source_name, exc, plugin=plugin)
        return source_name, [], error


async def _build_preview_results(
    db_pool: asyncpg.Pool, deduped: list[PaperCreate], user_id: int
) -> list[SearchPreviewResult]:
    """Attach local-library match metadata to each deduped preview result."""
    library_indexes, title_year_candidates = await search_helpers._load_local_library_matches(
        db_pool, deduped, user_id
    )
    return [
        SearchPreviewResult(
            **paper.model_dump(),
            library_match=search_helpers._match_preview_result(
                paper, library_indexes, title_year_candidates
            ),
        )
        for paper in deduped
    ]


@router.post("/search-preview", response_model=SearchPreviewResponse)
@limiter.limit("30/minute")
async def search_papers_preview(
    request: Request,
    body: SearchRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    user_id: int = Depends(get_current_user_id),
) -> SearchPreviewResponse:
    """Search papers without saving to DB -- for preview & select flow.

    Supports multi-source fan-out; returns deduplicated results with
    per_source_counts and degraded_sources metadata.
    """
    if not body.source_types:
        raise HTTPException(status_code=400, detail="At least one source must be selected")
    source_types = body.source_types
    n = len(source_types)
    base_per_source = max(1, body.max_results // n)
    budgets = [base_per_source] * n
    remainder = body.max_results - base_per_source * n
    for i in range(remainder):
        budgets[i] += 1

    # Resolve source instances
    resolved_plugins, source_load_errors = await _resolve_sources_for_search(
        source_types, db_pool, http_client, request
    )
    plugins, degraded_sources, source_errors = _collect_preview_available_plugins(
        source_types, budgets, resolved_plugins, source_load_errors
    )

    if not plugins:
        # All sources failed to load — raise using first source's type for compat
        raise HTTPException(status_code=400, detail="No sources available for search")

    # Fan-out search concurrently
    raw_results = await asyncio.gather(
        *[
            _search_preview_source_papers(name, plugin, budget, body)
            for name, plugin, budget in plugins
        ]
    )

    per_source: dict[str, list[PaperCreate]] = {}
    for source_name, papers, error in raw_results:
        per_source[source_name] = papers
        if error is not None:
            source_errors[source_name] = error
            degraded_sources.append(source_name)

    deduped = _merge_search_results(per_source, body.sort_by)

    preview_results = await _build_preview_results(db_pool, deduped, user_id)

    per_source_counts = _count_results_by_source(per_source, deduped)

    return SearchPreviewResponse(
        results=preview_results,
        total=len(deduped),
        per_source_counts=per_source_counts,
        degraded_sources=list(source_errors.keys()),
        source_errors=source_errors,
    )


# ---------------------------------------------------------------------------
# POST /api/papers/search-hybrid
# ---------------------------------------------------------------------------


@router.post("/papers/search-hybrid", response_model=list[HybridSearchResult])
@limiter.limit("30/minute")
async def search_hybrid(
    request: Request,
    body: SearchRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder = Depends(get_embedder),
    user_id: int = Depends(get_current_user_id),
) -> list[dict]:
    """Search papers using hybrid BM25 + semantic search with RRF fusion.

    Combines PostgreSQL full-text search (BM25) and Qdrant semantic
    similarity via Reciprocal Rank Fusion.  Returns results ranked by
    fused relevance score.

    Parameters
    ----------
    body : SearchRequest
        ``query`` (str) and ``max_results`` (int, default 10).

    Returns
    -------
    list[dict]
        Papers with ``id``, ``title``, ``authors``, ``url``, ``abstract``,
        ``published_date``, ``rrf_score``, ``bm25_rank``, ``semantic_rank``.
    """
    if embedder.qdrant is None:
        raise HTTPException(
            status_code=503,
            detail="Qdrant unavailable for hybrid search",
        )

    results = await embedder.hybrid_search(
        body.query, db_pool, limit=body.max_results, user_id=user_id
    )
    return results


# ---------------------------------------------------------------------------
# POST /api/relevance-score
# ---------------------------------------------------------------------------


@router.post("/relevance-score", response_model=RelevanceScoreResponse)
@limiter.limit("30/minute")
async def compute_relevance(
    request: Request,
    paper_id: int = Query(...),
    topic_id: int = Query(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
    user_id: int = Depends(get_current_user_id),
) -> dict[str, int | float]:
    """Compute and store relevance score between a paper and a topic."""
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        # Fetch paper and topic data in one round-trip
        paper = await conn.fetchrow("SELECT title, abstract FROM papers WHERE id = $1", paper_id)
        topic = await conn.fetchrow("SELECT query_terms FROM topics WHERE id = $1", topic_id)
        if not paper:
            raise HTTPException(404, f"Paper {paper_id} not found")
        if not topic:
            raise HTTPException(404, f"Topic {topic_id} not found")

        paper_text = f"{paper['title']}. {paper['abstract'] or ''}"
        if embedder is None or embedder.qdrant is None:
            raise HTTPException(status_code=503, detail="Search service unavailable")
        score = await embedder.compute_relevance(paper_text, topic["query_terms"])

        try:
            await conn.execute(
                """INSERT INTO paper_topics (paper_id, topic_id, relevance_score)
                VALUES ($1, $2, $3)
                ON CONFLICT (paper_id, topic_id) DO UPDATE SET relevance_score = $3""",
                paper_id,
                topic_id,
                score,
            )
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(404, "Referenced paper or topic not found")

    return {"paper_id": paper_id, "topic_id": topic_id, "relevance_score": score}
