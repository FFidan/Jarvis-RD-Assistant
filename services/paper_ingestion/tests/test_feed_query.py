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
    """The feed query builder should keep bind parameters aligned with filters."""
    query_parts = build_feed_queries(
        unread_only=True,
        sort="priority",
        limit=10,
        offset=20,
        q="attention",
        statuses="new,reading",
        source_types="arxiv",
        topic_names="agents,rag",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 1),
    )

    # WS-6B-β prepends a user-scoping predicate at $1 so all subsequent
    # placeholders shift +1.  In single-user mode (user_id=None) the predicate
    # short-circuits to TRUE.
    assert "($1::int IS NULL OR p.user_id IS NULL OR p.user_id = $1)" in query_parts.data_query
    assert "pus.user_id IS NOT DISTINCT FROM $1" in query_parts.data_query
    assert "COALESCE(pus.status, 'new') != 'read'" in query_parts.data_query
    assert "COALESCE(pus.archived, FALSE)" in query_parts.data_query
    assert "websearch_to_tsquery" in query_parts.data_query
    assert "END IN ($3, $4)" in query_parts.data_query
    assert "p.source_type IN ($5)" in query_parts.data_query
    assert "t.name = ANY($6::text[])" in query_parts.data_query
    assert "p.created_at >= $7" in query_parts.data_query
    assert "p.created_at <= $8" in query_parts.data_query
    assert "ORDER BY p.priority_score DESC NULLS LAST" in query_parts.data_query
    assert query_parts.params == [
        None,  # user_id (single-user default)
        "attention",
        "new",
        "reading",
        "arxiv",
        ["agents", "rag"],
        date(2026, 1, 1),
        date(2026, 3, 1),
        10,
        20,
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
    """WS-6B-β: passing user_id binds it to the user-scoping predicate at $1."""
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

    assert "($1::int IS NULL OR p.user_id IS NULL OR p.user_id = $1)" in query_parts.data_query
    # user_id is bound at $1, then LIMIT ($2) and OFFSET ($3)
    assert query_parts.params == [42, 10, 0]
    assert query_parts.count_params == [42]
