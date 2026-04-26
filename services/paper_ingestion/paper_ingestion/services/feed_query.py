"""Helpers for building and executing feed queries."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import asyncpg

logger = logging.getLogger(__name__)


def _select_sql(*, note_query_param: int | None, include_tldr: bool) -> str:
    tldr_sql = "ps.tldr" if include_tldr else "NULL AS tldr"
    if note_query_param is None:
        note_sql = " 0::integer AS note_match_count, NULL::text AS note_snippet,"
    else:
        note_sql = (
            " (SELECT COUNT(*)::integer FROM paper_notes pn"
            " WHERE pn.paper_id = p.id AND pn.source = 'zotero'"
            " AND to_tsvector('english', coalesce(pn.user_note, '') || ' '"
            " || coalesce(pn.highlight_text, '')) @@ websearch_to_tsquery('english',"
            f" ${note_query_param})) AS note_match_count,"
            " (SELECT NULLIF(left(coalesce(pn.highlight_text, pn.user_note), 240), '')"
            " FROM paper_notes pn"
            " WHERE pn.paper_id = p.id AND pn.source = 'zotero'"
            " AND to_tsvector('english', coalesce(pn.user_note, '') || ' '"
            " || coalesce(pn.highlight_text, '')) @@ websearch_to_tsquery('english',"
            f" ${note_query_param})"
            " ORDER BY pn.created_at DESC"
            " LIMIT 1) AS note_snippet,"
        )
    return (
        f"SELECT p.*, ps.summary_brief, {tldr_sql}, ps.confidence,"
        f"{note_sql}"
        " pus.status AS user_status, pus.rating,"
        " (EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)) AS has_chunks,"
        " (ps.id IS NOT NULL) AS has_summary,"
        " pr.score AS recommendation_score,"
        " pr.explanation AS recommendation_reason,"
        " pr.modes AS recommendation_modes"
    )


_BASE_SELECT = (
    "SELECT p.*, ps.summary_brief, ps.tldr, ps.confidence,"
    " 0::integer AS note_match_count, NULL::text AS note_snippet,"
    " pus.status AS user_status, pus.rating,"
    " (EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)) AS has_chunks,"
    " (ps.id IS NOT NULL) AS has_summary,"
    " pr.score AS recommendation_score,"
    " pr.explanation AS recommendation_reason,"
    " pr.modes AS recommendation_modes"
)
_FALLBACK_SELECT = (
    "SELECT p.*, ps.summary_brief, NULL AS tldr, ps.confidence,"
    " 0::integer AS note_match_count, NULL::text AS note_snippet,"
    " pus.status AS user_status, pus.rating,"
    " (EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)) AS has_chunks,"
    " (ps.id IS NOT NULL) AS has_summary,"
    " pr.score AS recommendation_score,"
    " pr.explanation AS recommendation_reason,"
    " pr.modes AS recommendation_modes"
)
_BASE_FROM = (
    " FROM papers p"
    " LEFT JOIN paper_summaries ps ON p.id = ps.paper_id"
    " LEFT JOIN paper_user_state pus ON p.id = pus.paper_id"
    " LEFT JOIN paper_recommendations pr ON pr.paper_id = p.id AND pr.dismissed = FALSE"
)


@dataclass(slots=True)
class FeedQueryParts:
    """Prepared SQL fragments and parameter bindings for the paper feed."""

    data_query: str
    fallback_data_query: str
    count_query: str
    params: list[object]
    count_params: list[object]


def split_csv_filter(raw_value: str | None) -> list[str]:
    """Normalize a comma-separated query parameter into trimmed values."""
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def build_feed_queries(
    *,
    unread_only: bool,
    sort: str,
    limit: int,
    offset: int,
    q: str | None,
    statuses: str | None,
    source_types: str | None,
    topic_names: str | None,
    date_from: date | None,
    date_to: date | None,
    recommended: bool = False,
    include_zotero_notes: bool = False,
    user_id: int | None = None,
) -> FeedQueryParts:
    """Build the feed data and count queries for the requested filters.

    The optional ``user_id`` filter scopes returned papers to the caller plus
    system-owned (``papers.user_id IS NULL``) rows.  When ``user_id`` is
    ``None`` (single-user mode) the predicate is a no-op and all papers are
    returned.
    """
    conditions: list[str] = []
    params: list[object] = []
    param_idx = 1

    # Multi-tenant scoping: caller's papers + system-owned (NULL).  In
    # single-user mode (user_id=None) the predicate short-circuits to TRUE.
    conditions.append(
        f"(${param_idx}::int IS NULL OR p.user_id IS NULL OR p.user_id = ${param_idx})"
    )
    params.append(user_id)
    param_idx += 1

    if unread_only:
        conditions.append("COALESCE(pus.status, 'new') != 'read'")

    note_query_param: int | None = None
    if q:
        if include_zotero_notes:
            note_query_param = param_idx
            conditions.append(
                "("
                f"p.search_vector @@ websearch_to_tsquery('english', ${param_idx})"
                " OR EXISTS (SELECT 1 FROM paper_notes pn"
                " WHERE pn.paper_id = p.id AND pn.source = 'zotero'"
                " AND to_tsvector('english', coalesce(pn.user_note, '') || ' '"
                " || coalesce(pn.highlight_text, '')) @@ websearch_to_tsquery('english',"
                f" ${param_idx}))"
                ")"
            )
        else:
            conditions.append(f"p.search_vector @@ websearch_to_tsquery('english', ${param_idx})")
        params.append(q)
        param_idx += 1

    status_list = split_csv_filter(statuses)
    if status_list:
        placeholders = ", ".join(f"${param_idx + i}" for i in range(len(status_list)))
        conditions.append(f"COALESCE(pus.status, 'new') IN ({placeholders})")
        params.extend(status_list)
        param_idx += len(status_list)

    source_list = split_csv_filter(source_types)
    if source_list:
        placeholders = ", ".join(f"${param_idx + i}" for i in range(len(source_list)))
        conditions.append(f"p.source_type IN ({placeholders})")
        params.extend(source_list)
        param_idx += len(source_list)

    topic_list = split_csv_filter(topic_names)
    if topic_list:
        conditions.append(
            "p.id IN (SELECT pt.paper_id FROM paper_topics pt"
            " JOIN topics t ON pt.topic_id = t.id"
            f" WHERE t.name = ANY(${param_idx}::text[]))"
        )
        params.append(topic_list)
        param_idx += 1

    if date_from:
        conditions.append(f"p.created_at >= ${param_idx}")
        params.append(date_from)
        param_idx += 1

    if date_to:
        conditions.append(f"p.created_at <= ${param_idx}")
        params.append(date_to)
        param_idx += 1

    if recommended:
        conditions.append("pr.id IS NOT NULL")

    where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sort_map = {
        "priority": " ORDER BY p.priority_score DESC NULLS LAST",
        "published_date": " ORDER BY p.published_date DESC NULLS LAST",
        "title": " ORDER BY p.title ASC",
        "citation_count": " ORDER BY p.citation_count DESC NULLS LAST",
        "recommendation": " ORDER BY pr.score DESC NULLS LAST",
    }
    order_sql = sort_map.get(sort, " ORDER BY p.discovered_at DESC")

    count_params = list(params)
    base_select = _select_sql(note_query_param=note_query_param, include_tldr=True)
    fallback_select = _select_sql(note_query_param=note_query_param, include_tldr=False)
    data_query = (
        f"{base_select}{_BASE_FROM}{where_sql}{order_sql}"
        f" LIMIT ${param_idx} OFFSET ${param_idx + 1}"
    )
    fallback_data_query = (
        f"{fallback_select}{_BASE_FROM}{where_sql}{order_sql}"
        f" LIMIT ${param_idx} OFFSET ${param_idx + 1}"
    )
    params.extend([limit, offset])
    count_query = f"SELECT COUNT(*) AS total{_BASE_FROM}{where_sql}"

    return FeedQueryParts(
        data_query=data_query,
        fallback_data_query=fallback_data_query,
        count_query=count_query,
        params=params,
        count_params=count_params,
    )


async def fetch_feed_rows(
    conn,
    query_parts: FeedQueryParts,
):
    """Fetch feed rows, retrying without TLDR if the column is absent."""
    try:
        return await conn.fetch(query_parts.data_query, *query_parts.params)
    except asyncpg.exceptions.UndefinedColumnError:
        logger.warning("Column ps.tldr missing; retrying feed query without TLDR")
        return await conn.fetch(query_parts.fallback_data_query, *query_parts.params)


def derive_feed_search_mode(q: str | None) -> str:
    """Return the frontend search-mode marker for a feed response."""
    return "bm25" if q else "filtered"
