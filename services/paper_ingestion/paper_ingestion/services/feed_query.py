"""Helpers for building and executing feed queries."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import asyncpg

from paper_ingestion.queries.predicates import VIEW_PREDICATES

logger = logging.getLogger(__name__)


def _select_sql(*, note_query_param: int | None, include_tldr: bool) -> str:
    tldr_sql = "ps.tldr" if include_tldr else "NULL AS tldr"
    if note_query_param is None:
        note_sql = " 0::integer AS note_match_count, NULL::text AS note_snippet,"
    else:
        note_sql = (
            " (SELECT COUNT(*)::integer FROM paper_notes pn"
            " WHERE pn.paper_id = p.id AND pn.source = 'zotero'"
            " AND pn.user_id IS NOT DISTINCT FROM $1"
            " AND to_tsvector('english', coalesce(pn.user_note, '') || ' '"
            " || coalesce(pn.highlight_text, '')) @@ websearch_to_tsquery('english',"
            f" ${note_query_param})) AS note_match_count,"
            " (SELECT NULLIF(left(coalesce(pn.highlight_text, pn.user_note), 240), '')"
            " FROM paper_notes pn"
            " WHERE pn.paper_id = p.id AND pn.source = 'zotero'"
            " AND pn.user_id IS NOT DISTINCT FROM $1"
            " AND to_tsvector('english', coalesce(pn.user_note, '') || ' '"
            " || coalesce(pn.highlight_text, '')) @@ websearch_to_tsquery('english',"
            f" ${note_query_param})"
            " ORDER BY pn.created_at DESC"
            " LIMIT 1) AS note_snippet,"
        )
    return (
        f"SELECT p.*, ps.summary_brief, {tldr_sql}, ps.confidence,"
        f"{note_sql}"
        " COALESCE(pus.state, 'inbox') AS state,"
        " pus.state_before_trash,"
        " COALESCE(pus.starred, FALSE) AS starred,"
        " pus.rating,"
        " (EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)) AS has_chunks,"
        " (ps.id IS NOT NULL) AS has_summary,"
        " pr.score AS recommendation_score,"
        " pr.explanation AS recommendation_reason,"
        " pr.modes AS recommendation_modes"
    )


# Papers are canonical (no owner column); per-user library membership
# is in `user_library`. The feed joins `user_library` so users see
# *their* library; single-user mode (user_id=None) bypasses the join.
_BASE_FROM_USER = (
    " FROM papers p"
    " JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1"
    " LEFT JOIN paper_summaries ps ON p.id = ps.paper_id"
    " AND ps.user_id IS NOT DISTINCT FROM $1"
    " LEFT JOIN paper_user_state pus"
    " ON p.id = pus.paper_id AND pus.user_id IS NOT DISTINCT FROM $1"
    " LEFT JOIN paper_recommendations pr"
    " ON pr.paper_id = p.id AND pr.dismissed = FALSE"
    " AND pr.user_id IS NOT DISTINCT FROM $1"
)
_BASE_FROM_CORPUS_USER = (
    " FROM papers p"
    " LEFT JOIN paper_summaries ps ON p.id = ps.paper_id"
    " AND ps.user_id IS NOT DISTINCT FROM $1"
    " LEFT JOIN paper_user_state pus"
    " ON p.id = pus.paper_id AND pus.user_id IS NOT DISTINCT FROM $1"
    " LEFT JOIN paper_recommendations pr"
    " ON pr.paper_id = p.id AND pr.dismissed = FALSE"
    " AND pr.user_id IS NOT DISTINCT FROM $1"
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
    """Normalize a comma-separated query parameter into trimmed, non-empty values.

    Parameters
    ----------
    raw_value : str | None
        Raw query-parameter value, e.g. ``"arxiv,semantic_scholar"``.

    Returns
    -------
    list[str]
        Trimmed non-empty tokens.  Returns ``[]`` when *raw_value* is ``None``
        or consists entirely of whitespace/commas.
    """
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
    topic_id: int | None = None,
    untagged: bool = False,
    date_from: date | None,
    date_to: date | None,
    recommended: bool = False,
    include_zotero_notes: bool = False,
    user_id: int | None = None,
    view: str | None = None,
    scope: str = "library",
) -> FeedQueryParts:
    """Build the feed data and count queries for the requested filters.

    Papers are a canonical shared corpus; per-user library membership is
    in ``user_library``. The data query JOINs ``user_library`` so each
    user only sees what's actually in *their* library. When ``user_id``
    is ``None`` (single-user fallback) the JOIN is skipped and the query
    returns the entire canonical corpus.

    The ``view`` parameter maps to a set of fixed SQL predicates defined in
    :data:`VIEW_PREDICATES`.  Valid values:
    ``inbox / library / reading_list / reading / done / starred / trash /
    active / kept / all_non_trash``.
    When ``view`` is supplied it takes precedence over the legacy ``statuses``
    filter.  Raises :exc:`ValueError` if ``view`` is not a recognised key.

    .. deprecated::
        `statuses` is ignored. Use `view` instead. Will be removed in a future release.
    """
    if view is not None and view not in VIEW_PREDICATES:
        raise ValueError(f"Unknown view {view!r}. Valid values: {sorted(VIEW_PREDICATES)}")
    if scope not in {"library", "corpus"}:
        raise ValueError(f"Unknown scope {scope!r}. Valid values: ['corpus', 'library']")

    conditions: list[str] = []
    params: list[object] = []
    param_idx = 1

    # user_library JOIN replaces the legacy `p.user_id IS NULL OR p.user_id = $N`
    # predicate. The first parameter is always reserved for user_id so
    # downstream parameter numbering matches historical expectations (and the
    # LEFT JOIN onto paper_user_state still binds against $1).
    if user_id is None or scope == "corpus":
        # _BASE_FROM_CORPUS_USER binds user_id to $1 via IS NOT DISTINCT FROM $1
        # which evaluates to IS NULL when user_id is NULL — semantically identical
        # to the old _BASE_FROM_NO_USER literal. Keeping the $1 reference also
        # gives asyncpg/Postgres a type to resolve from `pus.user_id` (integer);
        # without it, an unused $1=NULL raised IndeterminateDatatypeError when
        # the SQL had only $2 LIMIT / $3 OFFSET refs.
        base_from = _BASE_FROM_CORPUS_USER
    else:
        base_from = _BASE_FROM_USER
    params.append(user_id)
    param_idx += 1

    note_query_param: int | None = None
    if q:
        if include_zotero_notes:
            note_query_param = param_idx
            conditions.append(
                "("
                f"p.search_vector @@ websearch_to_tsquery('english', ${param_idx})"
                " OR EXISTS (SELECT 1 FROM paper_notes pn"
                " WHERE pn.paper_id = p.id AND pn.source = 'zotero'"
                " AND pn.user_id IS NOT DISTINCT FROM $1"
                " AND to_tsvector('english', coalesce(pn.user_note, '') || ' '"
                " || coalesce(pn.highlight_text, '')) @@ websearch_to_tsquery('english',"
                f" ${param_idx}))"
                ")"
            )
        else:
            conditions.append(f"p.search_vector @@ websearch_to_tsquery('english', ${param_idx})")
        params.append(q)
        param_idx += 1

    # In corpus scope, the Library tab means "all non-trash canonical papers";
    # user library membership is only meaningful in library scope.
    effective_view = "all_non_trash" if scope == "corpus" and view == "library" else view

    # unread_only is only applied when no explicit view is requested; an
    # explicit view already encodes its own state predicate and must not be
    # contradicted by the active-state guard.
    if unread_only and effective_view is None:
        conditions.append(f"({VIEW_PREDICATES['active']})")

    # view= takes precedence over the legacy statuses= filter
    if effective_view is not None:
        conditions.append(f"({VIEW_PREDICATES[effective_view]})")
    else:
        status_list = split_csv_filter(statuses)
        if status_list:
            logger.warning(
                "feed_query: 'statuses' query param is deprecated and ignored "
                "post-Phase-A; use 'view' instead. Got: %s",
                status_list,
            )
            # Intentionally no condition added — statuses filter is dead.

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

    if topic_id is not None:
        conditions.append(
            f"p.id IN (SELECT pt.paper_id FROM paper_topics pt WHERE pt.topic_id = ${param_idx})"
        )
        params.append(topic_id)
        param_idx += 1

    if untagged:
        conditions.append("NOT EXISTS (SELECT 1 FROM paper_topics pt WHERE pt.paper_id = p.id)")

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
        f"{base_select}{base_from}{where_sql}{order_sql} LIMIT ${param_idx} OFFSET ${param_idx + 1}"
    )
    fallback_data_query = (
        f"{fallback_select}{base_from}{where_sql}{order_sql}"
        f" LIMIT ${param_idx} OFFSET ${param_idx + 1}"
    )
    params.extend([limit, offset])
    count_query = f"SELECT COUNT(*) AS total{base_from}{where_sql}"

    return FeedQueryParts(
        data_query=data_query,
        fallback_data_query=fallback_data_query,
        count_query=count_query,
        params=params,
        count_params=count_params,
    )


async def fetch_feed_rows(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    query_parts: FeedQueryParts,
) -> list[asyncpg.Record]:
    """Fetch feed rows, retrying without the TLDR column if it is absent.

    Parameters
    ----------
    conn : asyncpg.Connection
        Active database connection or pool proxy.
    query_parts : FeedQueryParts
        Pre-built SQL fragments and bound parameter list from
        :func:`build_feed_queries`.

    Returns
    -------
    list[asyncpg.Record]
        Feed rows with all available columns populated.

    Notes
    -----
    The ``ps.tldr`` column was added in a later migration.  The fallback
    query omits it so deployments that have not yet run that migration
    continue to work without a 500 error.
    """
    try:
        return await conn.fetch(query_parts.data_query, *query_parts.params)
    except asyncpg.exceptions.UndefinedColumnError:
        logger.warning("Column ps.tldr missing; retrying feed query without TLDR")
        return await conn.fetch(query_parts.fallback_data_query, *query_parts.params)


def derive_feed_search_mode(q: str | None) -> str:
    """Return the frontend search-mode marker for a feed response.

    Parameters
    ----------
    q : str | None
        Full-text search query from the request, or ``None`` when absent.

    Returns
    -------
    str
        ``"bm25"`` when a search query is present; ``"filtered"`` otherwise.
    """
    return "bm25" if q else "filtered"


# ---------------------------------------------------------------------------
# UI v3 facet-count helpers (§ Source / § Topic in the facet rail)
# ---------------------------------------------------------------------------

_SQL_BY_SOURCE_USER = """
    SELECT p.source_type, COUNT(*)::int AS cnt
      FROM papers p
      JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
     GROUP BY p.source_type
"""

_SQL_BY_SOURCE_CORPUS = """
    SELECT p.source_type, COUNT(*)::int AS cnt
      FROM papers p
     GROUP BY p.source_type
"""

_SQL_BY_TOPIC_USER = """
    SELECT t.id AS topic_id, t.name, COUNT(DISTINCT pt.paper_id)::int AS cnt
      FROM topics t
      JOIN paper_topics pt ON pt.topic_id = t.id
      JOIN user_library ul ON ul.paper_id = pt.paper_id AND ul.user_id = $1
     GROUP BY t.id, t.name
     ORDER BY cnt DESC, t.name
"""

_SQL_BY_TOPIC_CORPUS = """
    SELECT t.id AS topic_id, t.name, COUNT(DISTINCT pt.paper_id)::int AS cnt
      FROM topics t
      JOIN paper_topics pt ON pt.topic_id = t.id
     GROUP BY t.id, t.name
     ORDER BY cnt DESC, t.name
"""

_SQL_UNTAGGED_USER = """
    SELECT COUNT(*)::int AS cnt
      FROM papers p
      JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
     WHERE NOT EXISTS (
         SELECT 1 FROM paper_topics pt WHERE pt.paper_id = p.id
     )
"""

_SQL_UNTAGGED_CORPUS = """
    SELECT COUNT(*)::int AS cnt
      FROM papers p
     WHERE NOT EXISTS (
         SELECT 1 FROM paper_topics pt WHERE pt.paper_id = p.id
     )
"""


async def fetch_feed_facet_counts(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    user_id: int | None,
    scope: str = "library",
) -> tuple[dict[str, int], list[dict[str, Any]], int]:
    """Return (by_source, by_topic_rows, untagged) facet counts.

    When *scope* is ``"library"`` (default) and *user_id* is not None, the
    three aggregations are scoped to that user's user_library rows.  When
    *scope* is ``"corpus"`` — or when *user_id* is None (no-auth mode) —
    the corpus-wide SQL variants are used instead, mirroring the branching
    in ``get_feed_counts`` and ``build_feed_queries``.

    Returns:
        by_source  — ``{source_type: count}`` mapping.
        by_topic   — list of ``{topic_id, name, count}`` dicts, desc by count.
        untagged   — count of papers with no paper_topics row.
    """
    if scope == "corpus" or user_id is None:
        source_rows = await conn.fetch(_SQL_BY_SOURCE_CORPUS)
        topic_rows = await conn.fetch(_SQL_BY_TOPIC_CORPUS)
        untagged_row = await conn.fetchrow(_SQL_UNTAGGED_CORPUS)
    else:
        source_rows = await conn.fetch(_SQL_BY_SOURCE_USER, user_id)
        topic_rows = await conn.fetch(_SQL_BY_TOPIC_USER, user_id)
        untagged_row = await conn.fetchrow(_SQL_UNTAGGED_USER, user_id)

    by_source: dict[str, int] = {row["source_type"]: row["cnt"] for row in source_rows}
    by_topic: list[dict[str, Any]] = [
        {
            "topic_id": cast(int, row["topic_id"]),
            "name": cast(str, row["name"]),
            "count": cast(int, row["cnt"]),
        }
        for row in topic_rows
    ]
    untagged: int = untagged_row["cnt"] if untagged_row is not None else 0

    return by_source, by_topic, untagged
