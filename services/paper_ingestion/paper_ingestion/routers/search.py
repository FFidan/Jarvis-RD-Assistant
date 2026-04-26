"""Search, feed, discovery, and relevance scoring endpoints."""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from paper_ingestion.converters import (
    deduplicate_by_paper_id,
    row_to_feed_paper,
    row_to_paper_response,
)
from paper_ingestion.deps import get_db_pool, get_embedder, get_http_client, limiter
from paper_ingestion.embedder import Embedder
from paper_ingestion.models import (
    DiscoverRequest,
    DiscoveryResultItem,
    FeedResponse,
    HybridSearchResult,
    PaperCreate,
    PaperResponse,
    RelevanceScoreResponse,
    SearchRequest,
    SimilarPaperResult,
    priority_level,
)
from paper_ingestion.services.feed_query import (
    build_feed_queries,
    derive_feed_search_mode,
    fetch_feed_rows,
)
from paper_ingestion.services.pdf_workflow import upsert_paper
from paper_ingestion.services.source_helper import get_source_for_type

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])


# ---------------------------------------------------------------------------
# Response models for multi-source search
# ---------------------------------------------------------------------------


class MultiSourceSearchResponse(BaseModel):
    """Response for multi-source search endpoints."""

    results: list[PaperCreate]
    total: int
    per_source_counts: dict[str, int]
    degraded_sources: list[str]


class SearchPreviewLibraryMatch(BaseModel):
    """Local-library linkage metadata attached to preview search rows."""

    paper_id: int
    has_project_links: bool
    zotero_item_key: str | None


class SearchPreviewSourceError(BaseModel):
    """Structured per-source error details for preview searches."""

    kind: Literal["rate_limit", "api_error", "unavailable"]
    message: str
    status_code: int | None
    retry_after_s: int | None
    settings_hint: str | None


class SearchPreviewResult(PaperCreate):
    """Search preview result enriched with local-library metadata."""

    library_match: SearchPreviewLibraryMatch | None = None


class SearchPreviewResponse(BaseModel):
    """Response for POST /api/search-preview."""

    results: list[SearchPreviewResult]
    total: int
    per_source_counts: dict[str, int]
    degraded_sources: list[str]
    source_errors: dict[str, SearchPreviewSourceError]


# Only downgrade expected source bootstrap/configuration failures. Programming bugs
# should still surface so they are not hidden as degraded search state.
PREVIEW_SOURCE_BOOTSTRAP_EXCEPTIONS = (TypeError, ValueError, ValidationError)

_SOURCE_DISPLAY_NAMES = {
    "arxiv": "arXiv",
    "openalex": "OpenAlex",
    "pubmed": "PubMed",
    "semantic_scholar": "Semantic Scholar",
}


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for dedup comparison."""
    normalized = title.lower()
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    return " ".join(normalized.split())


def _dedup_papers(papers: list[PaperCreate]) -> list[PaperCreate]:
    """Deduplicate papers by (doi or arxiv_id or (normalized_title, year)).

    First occurrence wins (preserves per-source relevance order).
    """
    seen: set[Any] = set()
    result: list[PaperCreate] = []
    for paper in papers:
        doi = paper.metadata.get("doi")
        arxiv_id = paper.metadata.get("arxiv_id")
        if doi:
            key: Any = ("doi", doi.lower())
        elif arxiv_id:
            key = ("arxiv", arxiv_id.lower())
        else:
            year = paper.published_date.year if paper.published_date else None
            key = ("title", _normalize_title(paper.title), year)
        if key not in seen:
            seen.add(key)
            result.append(paper)
    return result


def _round_robin_merge(per_source: dict[str, list[PaperCreate]]) -> list[PaperCreate]:
    """Round-robin interleave results across sources to preserve per-source order."""
    iters = [iter(papers) for papers in per_source.values() if papers]
    merged: list[PaperCreate] = []
    while iters:
        exhausted = []
        for it in iters:
            try:
                merged.append(next(it))
            except StopIteration:
                exhausted.append(it)
        for it in exhausted:
            iters.remove(it)
    return merged


def _raise_source_search_error(
    source_type: str, exc: httpx.HTTPStatusError, *, api_key_configured: bool
) -> NoReturn:
    """Translate source API failures into stable user-facing HTTP errors."""
    status_code = exc.response.status_code
    if source_type == "semantic_scholar" and status_code == 429:
        detail = "Semantic Scholar rate limit reached. Retry later"
        if not api_key_configured:
            detail += " or configure an API key in Settings > Sources."
        raise HTTPException(status_code=429, detail=detail) from exc
    raise HTTPException(status_code=502, detail=f"Source API error: {status_code}") from exc


def _normalize_url(url: str) -> str:
    """Canonicalize a URL for exact comparison.

    We normalize the scheme/netloc casing and remove trailing path slashes so
    equivalent canonical URLs compare cleanly without trying to guess at deeper
    URL semantics.
    """
    parsed = urlsplit(url.strip())
    if not parsed.scheme and not parsed.netloc:
        return url.strip().rstrip("/")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _normalize_author_name(author: str) -> str:
    """Canonicalize an author string for exact-overlap matching."""
    normalized = author.lower()
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    return " ".join(normalized.split())


def _normalize_authors(authors: Any) -> frozenset[str]:
    """Return normalized author strings suitable for overlap checks."""
    if not isinstance(authors, list):
        return frozenset()
    return frozenset(
        _normalize_author_name(str(author)) for author in authors if str(author).strip()
    )


def _source_display_name(source_name: str) -> str:
    """Return a human-readable source label for user-facing error messages."""
    return _SOURCE_DISPLAY_NAMES.get(source_name, source_name.replace("_", " ").title())


def _library_match_priority(row: Any) -> tuple[int, int, int]:
    """Rank duplicate local rows by actionability, then recency.

    Preference order:
    1. project-linked rows
    2. rows with a Zotero item key
    3. newest paper id
    """
    return (
        int(bool(row.get("has_project_links"))),
        int(bool(row.get("zotero_item_key"))),
        int(row["id"]),
    )


@dataclass(slots=True)
class _TitleYearLibraryCandidate:
    """Local-library candidate for the author-aware title/year fallback."""

    match: SearchPreviewLibraryMatch
    priority: tuple[int, int, int]
    authors: frozenset[str]


def _store_preferred_library_match(
    indexes: dict[tuple[str, Any], SearchPreviewLibraryMatch],
    priorities: dict[tuple[str, Any], tuple[int, int, int]],
    key: tuple[str, Any],
    row: Any,
    match: SearchPreviewLibraryMatch,
) -> None:
    """Store the best local match for a lookup key using deterministic tie-breaking."""
    priority = _library_match_priority(row)
    if priorities.get(key) is None or priority > priorities[key]:
        priorities[key] = priority
        indexes[key] = match


def _retry_after_seconds(exc: Exception) -> int | None:
    """Extract an integer Retry-After header when the upstream provided one."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        return int(float(retry_after))
    except (TypeError, ValueError):
        return None


def _semantic_scholar_api_key_configured(plugin: Any) -> bool:
    """Return True when the Semantic Scholar source appears to have an API key."""
    config_obj = getattr(getattr(plugin, "config", None), "config", None)
    if isinstance(config_obj, dict) and config_obj.get("api_key"):
        return True
    return bool(os.environ.get("SEMANTIC_SCHOLAR_API_KEY"))


def _build_preview_source_error(
    source_name: str,
    exc: Exception,
    *,
    plugin: Any | None = None,
    unavailable: bool = False,
) -> SearchPreviewSourceError:
    """Translate preview fan-out failures into structured error details."""
    if unavailable:
        message = str(getattr(exc, "detail", exc))
        return SearchPreviewSourceError(
            kind="unavailable",
            message=message,
            status_code=getattr(exc, "status_code", None),
            retry_after_s=None,
            settings_hint=(
                "Enable the source in Settings > Sources."
                if "disabled" in message.lower()
                else None
            ),
        )

    status_code = None
    retry_after_s = None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        retry_after_s = _retry_after_seconds(exc)

        if status_code == 429:
            message = f"{_source_display_name(source_name)} rate limit reached. Retry later"
            settings_hint = None
            if source_name == "semantic_scholar" and not _semantic_scholar_api_key_configured(
                plugin
            ):
                message += " or configure an API key in Settings > Sources."
                settings_hint = "Configure a Semantic Scholar API key in Settings > Sources."
            return SearchPreviewSourceError(
                kind="rate_limit",
                message=message,
                status_code=status_code,
                retry_after_s=retry_after_s,
                settings_hint=settings_hint,
            )

        return SearchPreviewSourceError(
            kind="api_error",
            message=f"Source API error: {status_code}",
            status_code=status_code,
            retry_after_s=retry_after_s,
            settings_hint=None,
        )

    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        message = str(exc.detail)
    else:
        message = str(exc) or f"{source_name} search failed"

    return SearchPreviewSourceError(
        kind="api_error",
        message=message,
        status_code=status_code,
        retry_after_s=None,
        settings_hint=None,
    )


async def _load_local_library_matches(
    db_pool: asyncpg.Pool,
) -> tuple[
    dict[tuple[str, Any], SearchPreviewLibraryMatch],
    dict[tuple[str, int], list[_TitleYearLibraryCandidate]],
]:
    """Fetch local-library rows once and index them by every supported match key."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id,
                   p.external_id,
                   p.title,
                   p.authors,
                   p.published_date,
                   p.url,
                   p.metadata,
                   p.zotero_item_key,
                   EXISTS (
                       SELECT 1
                       FROM project_papers pp
                       WHERE pp.paper_id = p.id
                   ) AS has_project_links
            FROM papers p
            ORDER BY p.id ASC
            """
        )

    indexes: dict[tuple[str, Any], SearchPreviewLibraryMatch] = {}
    priorities: dict[tuple[str, Any], tuple[int, int, int]] = {}
    title_year_candidates: dict[tuple[str, int], list[_TitleYearLibraryCandidate]] = {}
    for row in rows:
        metadata = row.get("metadata") or {}
        match = SearchPreviewLibraryMatch(
            paper_id=row["id"],
            has_project_links=bool(row.get("has_project_links")),
            zotero_item_key=row.get("zotero_item_key") or None,
        )
        priority = _library_match_priority(row)

        doi = metadata.get("doi")
        if doi:
            _store_preferred_library_match(
                indexes, priorities, ("doi", str(doi).strip().lower()), row, match
            )

        arxiv_id = metadata.get("arxiv_id")
        if arxiv_id:
            _store_preferred_library_match(
                indexes, priorities, ("arxiv_id", str(arxiv_id).strip().lower()), row, match
            )

        normalized_url = _normalize_url(str(row["url"]))
        _store_preferred_library_match(indexes, priorities, ("url", normalized_url), row, match)
        _store_preferred_library_match(
            indexes,
            priorities,
            ("external_id", str(row["external_id"]).strip().lower()),
            row,
            match,
        )

        published_date = row.get("published_date")
        if published_date is not None:
            title_year = (_normalize_title(str(row["title"])), published_date.year)
            title_year_candidates.setdefault(title_year, []).append(
                _TitleYearLibraryCandidate(
                    match=match,
                    priority=priority,
                    authors=_normalize_authors(row.get("authors")),
                )
            )

    return indexes, title_year_candidates


def _match_preview_result(
    paper: PaperCreate,
    library_indexes: dict[tuple[str, Any], SearchPreviewLibraryMatch],
    title_year_candidates: dict[tuple[str, int], list[_TitleYearLibraryCandidate]],
) -> SearchPreviewLibraryMatch | None:
    """Apply local-library matching precedence to a preview result."""
    metadata = paper.metadata or {}

    doi = metadata.get("doi")
    if doi:
        match = library_indexes.get(("doi", str(doi).strip().lower()))
        if match is not None:
            return match

    arxiv_id = metadata.get("arxiv_id")
    if arxiv_id:
        match = library_indexes.get(("arxiv_id", str(arxiv_id).strip().lower()))
        if match is not None:
            return match

    normalized_url = _normalize_url(paper.url)
    match = library_indexes.get(("url", normalized_url))
    if match is not None:
        return match

    match = library_indexes.get(("external_id", paper.external_id.strip().lower()))
    if match is not None:
        return match

    if paper.published_date is None:
        return None

    preview_authors = _normalize_authors(paper.authors)
    if not preview_authors:
        return None

    # Title/year alone is too weak; only use this fallback when authors overlap.
    title_year_key = (_normalize_title(paper.title), paper.published_date.year)
    candidates = title_year_candidates.get(title_year_key)
    if not candidates:
        return None

    matching_candidates = [
        candidate for candidate in candidates if candidate.authors.intersection(preview_authors)
    ]
    if not matching_candidates:
        return None

    return max(matching_candidates, key=lambda candidate: candidate.priority).match


# ---------------------------------------------------------------------------
# POST /api/search
# ---------------------------------------------------------------------------


@router.post("/search", response_model=MultiSourceSearchResponse)
@limiter.limit("30/minute")
async def search_papers(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
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
    plugins = []
    degraded_sources: list[str] = []
    for st, budget in zip(source_types, budgets):
        try:
            plugin = await get_source_for_type(st, db_pool, http_client, request=request)
            plugins.append((st.value, plugin, budget))
        except HTTPException as e:
            logger.warning("Source %s unavailable for search: %s", st.value, e.detail)
            degraded_sources.append(st.value)

    # Fan-out search across all available sources concurrently
    async def _search_one(
        source_name: str, plugin: Any, budget: int
    ) -> tuple[str, list[PaperCreate]]:
        try:
            papers = await plugin.search(
                body.query,
                budget,
                year_from=body.year_from,
                year_to=body.year_to,
                sort_by=body.sort_by,
                author=body.author,
            )
            return source_name, papers
        except Exception as exc:  # broad: heterogeneous plugins raise different exception types
            logger.warning("Source %s search failed: %s", source_name, exc, exc_info=True)
            return source_name, []

    raw_results = await asyncio.gather(*[_search_one(n, p, b) for n, p, b in plugins])

    # Collect per-source results, track errors
    per_source: dict[str, list[PaperCreate]] = {}
    for source_name, papers in raw_results:
        if not papers and source_name not in degraded_sources:
            # Empty but not an exception — still record counts
            per_source[source_name] = []
        else:
            per_source[source_name] = papers

    # Mark sources that errored (returned empty due to exception)
    for source_name, papers in raw_results:
        if not papers and source_name not in per_source:
            degraded_sources.append(source_name)

    # Merge: date sort → sort merged list; else round-robin interleave
    if body.sort_by == "date":
        all_papers: list[PaperCreate] = []
        for papers in per_source.values():
            all_papers.extend(papers)
        deduped = _dedup_papers(all_papers)
        deduped.sort(key=lambda p: p.published_date or date.min, reverse=True)
    else:
        interleaved = _round_robin_merge(per_source)
        deduped = _dedup_papers(interleaved)

    # Upsert into DB (per original /api/search behavior)
    saved_results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        for paper in deduped:
            row = await upsert_paper(conn, paper)
            saved_results.append(row_to_paper_response(row))

    # Build per_source_counts from deduped results
    per_source_counts: dict[str, int] = {}
    for source_name in per_source:
        per_source_counts[source_name] = sum(
            1 for p in deduped if p.source_type.value == source_name
        )

    return MultiSourceSearchResponse(
        results=deduped,
        total=len(deduped),
        per_source_counts=per_source_counts,
        degraded_sources=degraded_sources,
    )


@router.post("/search-preview", response_model=SearchPreviewResponse)
@limiter.limit("30/minute")
async def search_papers_preview(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
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
    plugins = []
    degraded_sources: list[str] = []
    source_errors: dict[str, SearchPreviewSourceError] = {}
    for st, budget in zip(source_types, budgets):
        try:
            plugin = await get_source_for_type(st, db_pool, http_client, request=request)
            plugins.append((st.value, plugin, budget))
        except HTTPException as e:
            logger.warning("Source %s unavailable for preview search: %s", st.value, e.detail)
            source_errors[st.value] = _build_preview_source_error(st.value, e, unavailable=True)
            degraded_sources.append(st.value)
        except PREVIEW_SOURCE_BOOTSTRAP_EXCEPTIONS as exc:
            logger.warning("Source %s unavailable for preview search: %s", st.value, exc)
            source_errors[st.value] = _build_preview_source_error(st.value, exc, unavailable=True)
            degraded_sources.append(st.value)

    if not plugins:
        # All sources failed to load — raise using first source's type for compat
        raise HTTPException(status_code=400, detail="No sources available for search")

    # Fan-out search concurrently
    async def _search_one(
        source_name: str, plugin: Any, budget: int
    ) -> tuple[str, list[PaperCreate], SearchPreviewSourceError | None]:
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
            error = _build_preview_source_error(source_name, exc, plugin=plugin)
            return source_name, [], error

    raw_results = await asyncio.gather(*[_search_one(n, p, b) for n, p, b in plugins])

    per_source: dict[str, list[PaperCreate]] = {}
    for source_name, papers, error in raw_results:
        per_source[source_name] = papers
        if error is not None:
            source_errors[source_name] = error
            degraded_sources.append(source_name)

    # Merge and dedup
    if body.sort_by == "date":
        all_papers: list[PaperCreate] = []
        for papers in per_source.values():
            all_papers.extend(papers)
        deduped = _dedup_papers(all_papers)
        deduped.sort(key=lambda p: p.published_date or date.min, reverse=True)
    else:
        interleaved = _round_robin_merge(per_source)
        deduped = _dedup_papers(interleaved)

    library_indexes, title_year_candidates = await _load_local_library_matches(db_pool)
    preview_results = [
        SearchPreviewResult(
            **paper.model_dump(),
            library_match=_match_preview_result(paper, library_indexes, title_year_candidates),
        )
        for paper in deduped
    ]

    per_source_counts: dict[str, int] = {}
    for source_name in per_source:
        per_source_counts[source_name] = sum(
            1 for p in deduped if p.source_type.value == source_name
        )

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
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder = Depends(get_embedder),
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

    results = await embedder.hybrid_search(body.query, db_pool, limit=body.max_results)
    return results


# ---------------------------------------------------------------------------
# GET /api/papers/feed
# ---------------------------------------------------------------------------


@router.get("/papers/feed", response_model=FeedResponse)
@limiter.limit("60/minute")
async def list_feed_papers(
    request: Request,
    unread_only: bool = False,
    sort: str = Query(
        default="discovered_at",
        pattern="^(discovered_at|priority|published_date|title|citation_count|recommendation)$",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None),
    statuses: str | None = Query(default=None),
    source_types: str | None = Query(default=None),
    topic_names: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    recommended: bool = False,
    include_zotero_notes: bool = Query(default=False),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> FeedResponse:
    """Return papers for the What's New feed.

    Parameters
    ----------
    request : Request
        FastAPI request (needed by rate limiter).
    unread_only : bool
        When True, return only unread papers.
    sort : str
        Sort order: ``discovered_at`` (newest first) or ``priority``
        (highest priority_score first).
    limit, offset : int
        Pagination parameters.
    q : str, optional
        Full-text search query.
    statuses : str, optional
        Comma-separated list of user statuses to filter by (e.g. ``new,reading``).
    source_types : str, optional
        Comma-separated list of source types (e.g. ``arxiv,semantic_scholar``).
    topic_names : str, optional
        Comma-separated list of topic names.
    date_from, date_to : date, optional
        Created-at date range boundaries.
    include_zotero_notes : bool
        Include Zotero-imported note/highlight full-text matches when ``q`` is set.
    db_pool : asyncpg.Pool
        Injected database pool.

    Returns
    -------
    FeedResponse
        ``{papers: [...], total: N}``
    """
    query_parts = build_feed_queries(
        unread_only=unread_only,
        sort=sort,
        limit=limit,
        offset=offset,
        q=q,
        statuses=statuses,
        source_types=source_types,
        topic_names=topic_names,
        date_from=date_from,
        date_to=date_to,
        recommended=recommended,
        include_zotero_notes=include_zotero_notes,
    )

    # Note: pool.acquire() without an explicit transaction uses auto-commit mode.
    # In asyncpg, a failed statement in auto-commit does NOT leave the connection
    # in an error state, so `conn` can safely be reused after the except clause.
    async with db_pool.acquire() as conn:
        rows = await fetch_feed_rows(conn, query_parts)
        count_row = await conn.fetchval(query_parts.count_query, *query_parts.count_params)

    papers = [row_to_feed_paper(row) for row in rows]
    for paper in papers:
        paper.priority_level = priority_level(paper.priority_score)

    return FeedResponse(
        papers=papers,
        total=count_row or 0,
        search_mode=derive_feed_search_mode(q),
    )


# ---------------------------------------------------------------------------
# GET /api/similar/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/similar/{paper_id}", response_model=list[SimilarPaperResult])
@limiter.limit("20/minute")
async def find_similar_papers(
    request: Request,
    paper_id: int,
    limit: int = Query(default=5, ge=1, le=20),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
) -> list[dict]:
    """Find papers semantically similar to the given paper.

    Uses Qdrant vector similarity search on paper chunk embeddings.

    Parameters
    ----------
    paper_id : int
        Database paper ID to find similar papers for.
    limit : int
        Maximum number of similar papers to return (1-20, default 5).

    Returns
    -------
    list[dict]
        Similar papers with similarity scores and matching snippets.
    """
    async with db_pool.acquire() as conn:
        paper_row = await conn.fetchrow(
            "SELECT id, title, abstract FROM papers WHERE id = $1", paper_id
        )
        if not paper_row:
            raise HTTPException(status_code=404, detail="Paper not found")

        title = paper_row["title"]
        abstract = paper_row["abstract"] or ""
        query_text = f"{title}. {abstract}"

        if embedder is None or embedder.qdrant is None:
            raise HTTPException(status_code=503, detail="Search service unavailable")
        results = await embedder.search_similar(
            query_text=query_text,
            limit=limit * 3,  # extra results for dedup
            paper_id_filter=paper_id,
            score_threshold=0.6,
        )

        # Deduplicate by paper_id, keep highest score per paper
        deduped = deduplicate_by_paper_id(results)

        # Sort by score descending
        sorted_results = sorted(deduped, key=lambda x: x["score"], reverse=True)
        sorted_results = sorted_results[:limit]

        # Enrich with paper metadata (batch query to avoid N+1)
        paper_ids = [r["paper_id"] for r in sorted_results]
        if paper_ids:
            meta_rows = await conn.fetch(
                "SELECT id, title, authors, url FROM papers WHERE id = ANY($1::int[])",
                paper_ids,
            )
            meta_map = {row["id"]: row for row in meta_rows}
        else:
            meta_map = {}

        enriched: list[dict] = []
        for r in sorted_results:
            meta = meta_map.get(r["paper_id"])
            if meta:
                enriched.append(
                    {
                        "paper_id": r["paper_id"],
                        "title": meta["title"],
                        "authors": meta["authors"],
                        "url": meta["url"],
                        "similarity_score": round(r["score"], 3),
                        "matching_snippet": r.get("content", ""),
                    }
                )

    return enriched


# ---------------------------------------------------------------------------
# POST /api/discover (seed-based discovery)
# ---------------------------------------------------------------------------


@router.post("/discover", response_model=list[DiscoveryResultItem])
@limiter.limit("10/minute")
async def discover_papers(
    request: Request,
    body: "DiscoverRequest",
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
) -> list[dict]:
    """Discover papers similar to a set of seed papers.

    Uses Qdrant's RecommendQuery with AVERAGE_VECTOR strategy to find
    papers that are semantically similar to the provided seeds.

    Parameters
    ----------
    body : DiscoverRequest
        Request with seed paper_ids, limit, and score_threshold.

    Returns
    -------
    list[dict]
        Discovered papers with metadata and similarity scores.
    """
    # Validate that all seed paper IDs exist
    async with db_pool.acquire() as conn:
        existing = await conn.fetch(
            "SELECT id FROM papers WHERE id = ANY($1::int[])", body.paper_ids
        )
        existing_ids = {row["id"] for row in existing}
        missing = [pid for pid in body.paper_ids if pid not in existing_ids]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"Papers not found: {missing}",
            )

    if embedder is None or embedder.qdrant is None:
        raise HTTPException(status_code=503, detail="Search service unavailable")
    results = await embedder.discover_from_seeds(
        seed_paper_ids=body.paper_ids,
        db_pool=db_pool,
        limit=body.limit,
        score_threshold=body.score_threshold,
    )

    if not results:
        return []

    # Enrich with paper metadata
    paper_ids = [r["paper_id"] for r in results]
    async with db_pool.acquire() as conn:
        meta_rows = await conn.fetch(
            "SELECT id, title, authors, url FROM papers WHERE id = ANY($1::int[])",
            paper_ids,
        )
    meta_map = {row["id"]: row for row in meta_rows}

    enriched: list[dict] = []
    for r in results:
        meta = meta_map.get(r["paper_id"])
        if meta:
            enriched.append(
                {
                    "paper_id": r["paper_id"],
                    "title": meta["title"],
                    "authors": meta["authors"],
                    "url": meta["url"],
                    "similarity_score": round(r["score"], 3),
                    "matching_snippet": r.get("content", ""),
                }
            )

    return enriched


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
):
    """Compute and store relevance score between a paper and a topic."""
    async with db_pool.acquire() as conn:
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
