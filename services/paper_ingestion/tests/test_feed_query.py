"""Direct tests for feed query helpers."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import asyncpg
import pytest
from paper_ingestion.services.feed_query import (
    build_feed_queries,
    derive_feed_search_mode,
    fetch_feed_rows,
    split_csv_filter,
)


def test_split_csv_filter_trims_and_discards_empty_items():
    """CSV filters should normalize user input before it reaches SQL builders."""
    assert split_csv_filter(" new, reading , ,archived ") == ["new", "reading", "archived"]
    assert split_csv_filter(None) == []


def test_build_feed_queries_collects_filters_and_pagination():
    """The feed query builder should keep bind parameters aligned with filters.

    Phase-A redesign: statuses= is deprecated/dead; the view= param replaces it.
    pus.state (not pus.status) is used throughout.

    Sprint B: with user_id supplied, the FROM clause JOINs ``user_library``;
    with user_id=None it falls back to canonical-corpus-only scope.
    """
    query_parts = build_feed_queries(
        unread_only=True,
        sort="priority",
        limit=10,
        offset=20,
        q="attention",
        statuses="new,reading",  # deprecated — ignored silently; no SQL generated
        source_types="arxiv",
        topic_names="agents,rag",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 1),
        user_id=42,
    )

    # Sprint B: user_library JOIN replaces the legacy `p.user_id IS NULL OR ...` predicate.
    assert (
        "JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1" in query_parts.data_query
    )
    # Phase-A: pus.user_id scoping is in _BASE_FROM (LEFT JOIN)
    assert "pus.user_id IS NOT DISTINCT FROM $1" in query_parts.data_query
    # unread_only → VIEW_PREDICATES['active'] (Phase-A state machine)
    assert "COALESCE(pus.state, 'inbox') IN ('inbox','to_read','reading')" in query_parts.data_query
    assert "websearch_to_tsquery" in query_parts.data_query

    # statuses= is dead post-Phase-A — no IN clause generated for it.
    # With statuses ignored: $2=q, $3=source, $4=topic_list, $5=date_from, $6=date_to
    assert "p.source_type IN ($3)" in query_parts.data_query
    assert "t.name = ANY($4::text[])" in query_parts.data_query
    assert "p.created_at >= $5" in query_parts.data_query
    assert "p.created_at <= $6" in query_parts.data_query
    assert "ORDER BY p.priority_score DESC NULLS LAST" in query_parts.data_query

    assert query_parts.params == [
        42,  # $1 user_id
        "attention",  # $2 q
        "arxiv",  # $3 source_type
        ["agents", "rag"],  # $4 topic_list
        date(2026, 1, 1),  # $5 date_from
        date(2026, 3, 1),  # $6 date_to
        10,  # $7 LIMIT
        20,  # $8 OFFSET
    ]
    assert query_parts.count_params == query_parts.params[:-2]


@pytest.mark.asyncio
async def test_fetch_feed_rows_retries_without_tldr_column():
    """Older schemas should still return feed rows when the TLDR column is absent."""
    conn = AsyncMock()
    conn.fetch.side_effect = [
        asyncpg.exceptions.UndefinedColumnError("missing tldr"),
        [{"id": 1}],
    ]
    query_parts = build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=5,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
    )

    rows = await fetch_feed_rows(conn, query_parts)

    assert rows == [{"id": 1}]
    assert conn.fetch.await_count == 2
    assert "NULL AS tldr" in conn.fetch.await_args_list[1].args[0]


def test_derive_feed_search_mode_marks_bm25_only_when_query_present():
    """Feed search mode should match the frontend contract."""
    assert derive_feed_search_mode("attention") == "bm25"
    assert derive_feed_search_mode(None) == "filtered"


def test_build_feed_queries_user_id_kwarg_threads_into_params():
    """Sprint B: passing user_id binds the user_library JOIN at $1."""
    query_parts = build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=10,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=42,
    )

    assert (
        "JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1" in query_parts.data_query
    )
    # user_id is bound at $1, then LIMIT ($2) and OFFSET ($3)
    assert query_parts.params == [42, 10, 0]
    assert query_parts.count_params == [42]


def test_build_feed_queries_corpus_scope_keeps_user_state_overlay_without_library_join():
    """Authenticated users can browse the canonical corpus without leaving their own state overlay."""
    query_parts = build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=10,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=42,
        scope="corpus",
    )

    assert "JOIN user_library" not in query_parts.data_query
    assert "pus.user_id IS NOT DISTINCT FROM $1" in query_parts.data_query
    assert query_parts.params == [42, 10, 0]
    assert query_parts.count_params == [42]


def test_build_feed_queries_scopes_zotero_note_search_to_caller():
    """Zotero note full-text search/snippets must not read another user's notes."""
    query_parts = build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=10,
        offset=0,
        q="highlight",
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        include_zotero_notes=True,
        user_id=42,
    )

    assert query_parts.data_query.count("pn.user_id IS NOT DISTINCT FROM $1") == 3
    assert query_parts.count_query.count("pn.user_id IS NOT DISTINCT FROM $1") == 1
    assert query_parts.params == [42, "highlight", 10, 0]


def test_build_feed_queries_scopes_recommendations_to_caller():
    """Recommendation labels/sorting are per-user state on shared canonical papers."""
    query_parts = build_feed_queries(
        unread_only=False,
        sort="recommendation",
        limit=10,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        recommended=True,
        user_id=42,
    )

    assert "pr.user_id IS NOT DISTINCT FROM $1" in query_parts.data_query
    assert "pr.user_id IS NOT DISTINCT FROM $1" in query_parts.count_query
    assert "pr.id IS NOT NULL" in query_parts.data_query


def test_build_feed_queries_corpus_library_view_means_all_non_trash():
    """All-discovered corpus view should not be limited to personal library states."""
    query_parts = build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=10,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=42,
        scope="corpus",
        view="library",
    )

    assert "JOIN user_library" not in query_parts.data_query
    assert "COALESCE(pus.state, 'inbox') != 'trash'" in query_parts.data_query
    assert "IN ('to_read','reading','done')" not in query_parts.data_query


def test_build_feed_queries_no_user_id_uses_canonical_corpus_fallback():
    """Sprint B: user_id=None bypasses the JOIN and returns the canonical corpus.

    This preserves single-tenant / pre-multi-user-mode behaviour where the
    feed shows every canonical paper regardless of library membership.
    """
    query_parts = build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=5,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=None,
    )

    assert "JOIN user_library" not in query_parts.data_query
    assert " FROM papers p" in query_parts.data_query


def test_build_feed_queries_params_align_with_placeholders_when_user_id_none():
    """Regression: every $N referenced in the SQL must have a matching param.

    Bug: when user_id=None and no filters were set, the SQL ended up with
    only $LIMIT / $OFFSET placeholders but params still contained an
    unused leading None (intended for $1). asyncpg's prepare step then
    raised IndeterminateDatatypeError: could not determine data type of
    parameter $1, returning 500 on every plain GET /api/papers/feed.

    The fix uses _BASE_FROM_CORPUS_USER (which DOES reference $1 via
    IS NOT DISTINCT FROM $1) so $1's type is resolvable from pus.user_id.
    This test asserts the contract directly: the SQL must reference every
    parameter index from 1..N where N == len(params).
    """
    import re

    query_parts = build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=5,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=None,
    )

    referenced = {int(m) for m in re.findall(r"\$(\d+)", query_parts.data_query)}
    n_params = len(query_parts.params)
    assert referenced == set(range(1, n_params + 1)), (
        f"SQL references {sorted(referenced)} but params has {n_params} entries. "
        f"Every $N from 1..{n_params} must appear in the SQL exactly when the "
        f"params list has N entries; unused params cause asyncpg "
        f"IndeterminateDatatypeError."
    )


# ---------------------------------------------------------------------------
# Canonical-corpus + user_library semantics (migrated from test_feed_query_canonical.py)
# ---------------------------------------------------------------------------


def _build_canonical(user_id: int | None):
    return build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=10,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=user_id,
    )


def test_user_id_present_uses_library_join():
    parts = _build_canonical(user_id=42)
    assert "JOIN user_library ul" in parts.data_query
    assert "ul.user_id = $1" in parts.data_query
    assert "p.user_id IS NULL" not in parts.data_query
    assert "p.discovered_by" not in parts.data_query


def test_user_id_none_falls_back_to_canonical_corpus():
    parts = _build_canonical(user_id=None)
    assert "JOIN user_library" not in parts.data_query
    assert " FROM papers p" in parts.data_query


def test_user_a_and_user_b_get_disjoint_param_lists():
    """Two callers building queries produce same SQL but different bound user_id."""
    a = _build_canonical(user_id=1)
    b = _build_canonical(user_id=2)
    assert a.data_query == b.data_query
    assert a.params[0] == 1
    assert b.params[0] == 2


def test_count_query_also_uses_library_join_when_user_id_set():
    parts = _build_canonical(user_id=99)
    assert "JOIN user_library" in parts.count_query
    assert "ul.user_id = $1" in parts.count_query


@pytest.mark.parametrize("uid", [None, 42])
def test_param_layout_starts_with_user_id_at_dollar1(uid):
    """First parameter is always user_id so LEFT JOIN onto paper_user_state binds $1."""
    parts = _build_canonical(user_id=uid)
    assert parts.params[0] is uid


# ---------------------------------------------------------------------------
# SQL predicate fragments (migrated from test_queries_predicates.py)
# ---------------------------------------------------------------------------


def test_view_predicates_has_ten_named_views() -> None:
    """The VIEW_PREDICATES dict must expose exactly the 10 spec §6 named views."""
    from paper_ingestion.queries.predicates import VIEW_PREDICATES

    assert set(VIEW_PREDICATES) == {
        "inbox",
        "library",
        "reading_list",
        "reading",
        "done",
        "starred",
        "trash",
        "active",
        "kept",
        "all_non_trash",
    }


def test_view_predicates_use_state_or_starred_column() -> None:
    """Every VIEW_PREDICATES entry must reference pus.state or pus.starred."""
    from paper_ingestion.queries.predicates import VIEW_PREDICATES

    for name, sql in VIEW_PREDICATES.items():
        assert ("pus.state" in sql) or ("pus.starred" in sql), (
            f"VIEW_PREDICATES[{name!r}] does not reference pus.state or pus.starred"
        )


def test_view_predicates_inbox_uses_coalesce() -> None:
    """Inbox view must default missing user_state rows to 'inbox'."""
    from paper_ingestion.queries.predicates import VIEW_PREDICATES

    assert VIEW_PREDICATES["inbox"] == "COALESCE(pus.state, 'inbox') = 'inbox'"


def test_view_predicates_library_includes_three_states() -> None:
    """Library view spans to_read / reading / done per spec §5.4."""
    from paper_ingestion.queries.predicates import VIEW_PREDICATES

    library_sql = VIEW_PREDICATES["library"]
    assert "to_read" in library_sql
    assert "reading" in library_sql
    assert "done" in library_sql


def test_view_predicates_starred_excludes_trash() -> None:
    """Starred view must exclude trashed papers per spec §2.4."""
    from paper_ingestion.queries.predicates import VIEW_PREDICATES

    starred_sql = VIEW_PREDICATES["starred"]
    assert "pus.starred = TRUE" in starred_sql
    assert "trash" in starred_sql


def test_view_predicates_trash_does_not_use_coalesce() -> None:
    """Trash view targets state='trash' directly; COALESCE is unnecessary."""
    from paper_ingestion.queries.predicates import VIEW_PREDICATES

    assert VIEW_PREDICATES["trash"] == "pus.state = 'trash'"


def test_recommender_exclude_sql_matches_spec() -> None:
    """Spec §7.3.1 — papers in trash or done are excluded from recommender output."""
    from paper_ingestion.queries.predicates import RECOMMENDER_EXCLUDE_SQL

    assert RECOMMENDER_EXCLUDE_SQL == "COALESCE(pus.state, 'inbox') IN ('trash','done')"


def test_pulse_candidate_exclude_sql_matches_spec() -> None:
    """Spec §6 + §7.3.1 — pulse candidate filter excludes trash and done."""
    from paper_ingestion.queries.predicates import PULSE_CANDIDATE_EXCLUDE_SQL

    assert PULSE_CANDIDATE_EXCLUDE_SQL == "COALESCE(pus.state, 'inbox') IN ('trash','done')"


def test_legacy_predicates_no_longer_importable() -> None:
    """Legacy IS_ARCHIVED_SQL et al. must be deleted (spec §11 atomic cutover)."""
    import paper_ingestion.queries.predicates as predicates_mod

    assert not hasattr(predicates_mod, "IS_ARCHIVED_SQL")
    assert not hasattr(predicates_mod, "IS_NOT_ARCHIVED_SQL")
    assert not hasattr(predicates_mod, "IS_DISMISSED_SQL")
    assert not hasattr(predicates_mod, "IS_SAVED_SQL")
