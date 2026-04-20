"""Helpers for building and executing feed queries."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import asyncpg

logger = logging.getLogger(__name__)

_BASE_SELECT = (
    "SELECT p.*, ps.summary_brief, ps.tldr, ps.confidence,"
    " pus.status AS user_status, pus.rating,"
    " (EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)) AS has_chunks,"
    " (ps.id IS NOT NULL) AS has_summary,"
    " pr.score AS recommendation_score,"
    " pr.explanation AS recommendation_reason,"
    " pr.modes AS recommendation_modes"
)
_FALLBACK_SELECT = (
    "SELECT p.*, ps.summary_brief, NULL AS tldr, ps.confidence,"
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
) -> FeedQueryParts:
    """Build the feed data and count queries for the requested filters."""
    conditions: list[str] = []
    params: list[object] = []
    param_idx = 1

    if unread_only:
        conditions.append("COALESCE(pus.status, 'new') != 'read'")

    if q:
        conditions.append(f"p.search_vector @@ plainto_tsquery('english', ${param_idx})")
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
    data_query = (
        f"{_BASE_SELECT}{_BASE_FROM}{where_sql}{order_sql}"
        f" LIMIT ${param_idx} OFFSET ${param_idx + 1}"
    )
    fallback_data_query = (
        f"{_FALLBACK_SELECT}{_BASE_FROM}{where_sql}{order_sql}"
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
