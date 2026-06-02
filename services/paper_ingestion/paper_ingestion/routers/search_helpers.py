"""Shared helpers and response models for the search router family.

Extracted from ``routers/search.py`` (GOD-001) so the helpers can be reused by
``routers/discovery.py`` and ``routers/feed.py`` without circular imports.
The originating module re-exports the public-by-convention names for
back-compat with tests that monkeypatch ``paper_ingestion.routers.search``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from paper_ingestion.models import PaperCreate
from paper_ingestion.sources.base import parse_retry_after

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
    """Lowercase, ASCII-normalize, collapse whitespace for dedup comparison.

    ASCII-only character class matches the SQL-side
    ``regexp_replace(... '[^[:alnum:]_[:space:]]', ' ', 'g')`` used in the
    title-year fallback. Cross-language matching is best-effort; non-ASCII
    characters become spaces on both sides so the candidate-key lookup and
    the in-memory index agree.
    """
    normalized = title.lower()
    normalized = re.sub(r"[^A-Za-z0-9_\s]", " ", normalized)
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


def _library_match_priority(row: asyncpg.Record) -> tuple[int, int, int]:
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


@dataclass(slots=True)
class _PreviewMatchKeys:
    """Deduplicated local-library lookup keys derived from preview results."""

    dois: set[str]
    arxiv_ids: set[str]
    urls: set[str]
    external_ids: set[str]
    normalized_titles: set[str]
    years: set[int]

    def has_keys(self) -> bool:
        """Return True when any key can narrow the local-library query."""
        return any(
            [
                self.dois,
                self.arxiv_ids,
                self.urls,
                self.external_ids,
                self.normalized_titles,
                self.years,
            ]
        )


def _preview_match_keys(papers: list[PaperCreate]) -> _PreviewMatchKeys:
    """Extract deduplicated local-library lookup keys from preview papers."""
    keys = _PreviewMatchKeys(
        dois=set(),
        arxiv_ids=set(),
        urls=set(),
        external_ids=set(),
        normalized_titles=set(),
        years=set(),
    )
    for paper in papers:
        metadata = paper.metadata or {}
        doi = metadata.get("doi")
        if doi:
            keys.dois.add(str(doi).strip().lower())

        arxiv_id = metadata.get("arxiv_id")
        if arxiv_id:
            keys.arxiv_ids.add(str(arxiv_id).strip().lower())

        if paper.url:
            keys.urls.add(_normalize_url(paper.url))

        if paper.external_id:
            keys.external_ids.add(paper.external_id.strip().lower())

        if paper.published_date is not None:
            keys.normalized_titles.add(_normalize_title(paper.title))
            keys.years.add(paper.published_date.year)
    return keys


def _store_preferred_library_match(
    indexes: dict[tuple[str, Any], SearchPreviewLibraryMatch],
    priorities: dict[tuple[str, Any], tuple[int, int, int]],
    key: tuple[str, Any],
    row: asyncpg.Record,
    match: SearchPreviewLibraryMatch,
) -> None:
    """Store the best local match for a lookup key using deterministic tie-breaking."""
    priority = _library_match_priority(row)
    if priorities.get(key) is None or priority > priorities[key]:
        priorities[key] = priority
        indexes[key] = match


def _semantic_scholar_api_key_configured(plugin: Any) -> bool:
    """Return True when the Semantic Scholar source appears to have an API key."""
    config_obj = getattr(getattr(plugin, "config", None), "config", None)
    if isinstance(config_obj, dict) and config_obj.get("api_key"):
        return True
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    return bool(get_paper_ingestion_settings().semantic_scholar_api_key)


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
        retry_after_s = parse_retry_after(exc)

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
    preview_papers: list[PaperCreate] | None = None,
    user_id: int | None = None,
) -> tuple[
    dict[tuple[str, Any], SearchPreviewLibraryMatch],
    dict[tuple[str, int], list[_TitleYearLibraryCandidate]],
]:
    """Fetch candidate local-library rows and index them by every supported match key.

    The optional ``user_id`` filter scopes results to the caller's owned papers
    plus system-owned (``user_id IS NULL``) rows.  When ``user_id`` is ``None``
    (single-user mode) the predicate is a no-op and all rows are returned.

    ``preview_papers`` narrows the query to rows that can match the preview
    result keys.  Older tests and internal callers may omit it; that preserves
    the legacy full-library scan.
    """
    keys = _preview_match_keys(preview_papers or [])
    if preview_papers is not None and not keys.has_keys():
        return {}, {}

    candidate_predicate = ""
    args: list[Any] = [user_id]
    if preview_papers is not None:
        args.extend(
            [
                sorted(keys.dois),
                sorted(keys.arxiv_ids),
                sorted(keys.urls),
                sorted(keys.external_ids),
                sorted(keys.normalized_titles),
                sorted(keys.years),
            ]
        )
        candidate_predicate = """
              AND (
                  lower(btrim(coalesce(p.metadata->>'doi', ''))) = ANY($2::text[])
                  OR lower(btrim(coalesce(p.metadata->>'arxiv_id', ''))) = ANY($3::text[])
                  OR btrim(
                      regexp_replace(
                          split_part(
                              split_part(lower(coalesce(p.url, '')), '#', 1),
                              '?',
                              1
                          ),
                          '/+$',
                          '',
                          'g'
                      )
                  ) = ANY($4::text[])
                  OR lower(btrim(p.external_id)) = ANY($5::text[])
                  OR (
                      btrim(
                          regexp_replace(
                              regexp_replace(lower(p.title), '[^[:alnum:]_[:space:]]', ' ', 'g'),
                              '[[:space:]]+',
                              ' ',
                              'g'
                          )
                      ) = ANY($6::text[])
                      AND EXTRACT(YEAR FROM p.published_date)::int = ANY($7::int[])
                  )
              )
        """

    # Scope library-preview to the caller's user_library when authenticated;
    # single-user fallback returns canonical-corpus matches.
    async with db_pool.acquire() as conn:
        if args[0] is not None:  # user_id present
            rows = await conn.fetch(
                f"""
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
                           JOIN projects pr ON pr.id = pp.project_id
                             AND ($1::bigint IS NULL OR pr.user_id IS NOT DISTINCT FROM $1)
                           WHERE pp.paper_id = p.id
                       ) AS has_project_links
                FROM papers p
                JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
                WHERE TRUE
                {candidate_predicate}
                ORDER BY p.id ASC
                """,
                *args,
            )
        else:
            # In single-user mode the leading $1 still consumes a parameter,
            # but we drop the predicate so all canonical papers are
            # candidates.
            rows = await conn.fetch(
                f"""
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
                           JOIN projects pr ON pr.id = pp.project_id
                             AND ($1::bigint IS NULL OR pr.user_id IS NOT DISTINCT FROM $1)
                           WHERE pp.paper_id = p.id
                       ) AS has_project_links
                FROM papers p
                WHERE ($1::int IS NULL OR TRUE)
                {candidate_predicate}
                ORDER BY p.id ASC
                """,
                *args,
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

        normalized_url = _normalize_url(str(row.get("url") or ""))
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

    match = library_indexes.get(("external_id", (paper.external_id or "").strip().lower()))
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
