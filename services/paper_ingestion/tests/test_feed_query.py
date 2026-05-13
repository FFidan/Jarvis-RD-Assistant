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
