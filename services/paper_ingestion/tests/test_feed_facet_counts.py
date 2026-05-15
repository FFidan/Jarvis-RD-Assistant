"""Tests for UI v3 feed facet-count aggregations (by_source / by_topic / untagged).

Covers:
- fetch_feed_facet_counts helper: user-scoped vs corpus-scope SQL selection.
- get_feed_counts router handler: facets are returned alongside the 10 named
  buckets.
- Cross-user isolation: user A's facets do NOT bleed into user B's counts.
- Untagged bucket: papers with no paper_topics row are counted correctly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from paper_ingestion.routers import papers
from paper_ingestion.services.feed_query import fetch_feed_facet_counts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pool(conn):
    """Wrap a mock conn in an asyncpg-style pool context manager."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _conn_with_txn():
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    return conn


def _make_pool_and_conn():
    conn = _conn_with_txn()
    pool = _pool(conn)
    return pool, conn


def _mock_request():
    return MagicMock()


def _ten_bucket_row():
    """Return a dict representing the 10-bucket aggregate fetchrow result."""
    return {
        "inbox": 5,
        "library": 10,
        "reading_list": 3,
        "reading": 2,
        "done": 4,
        "starred": 1,
        "trash": 0,
        "active": 14,
        "kept": 9,
        "all_non_trash": 24,
    }


# ---------------------------------------------------------------------------
# fetch_feed_facet_counts — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_feed_facet_counts_user_scoped_uses_user_library_join():
    """When user_id is provided, all three SQL queries reference user_library."""
    conn = AsyncMock()
    conn.fetch.side_effect = [
        # by_source rows
        [{"source_type": "arxiv", "cnt": 7}, {"source_type": "openalex", "cnt": 3}],
        # by_topic rows
        [{"topic_id": 1, "name": "RL", "cnt": 5}],
    ]
    conn.fetchrow.return_value = {"cnt": 2}  # untagged

    by_source, by_topic, untagged = await fetch_feed_facet_counts(conn, user_id=42)

    assert by_source == {"arxiv": 7, "openalex": 3}
    assert by_topic == [{"topic_id": 1, "name": "RL", "count": 5}]
    assert untagged == 2

    # Verify user_library JOIN present in the SQL passed to the db calls.
    fetch_calls = conn.fetch.call_args_list
    assert len(fetch_calls) == 2
    source_sql = fetch_calls[0].args[0]
    topic_sql = fetch_calls[1].args[0]
    assert "user_library" in source_sql, "by_source must join user_library"
    assert "user_library" in topic_sql, "by_topic must join user_library"

    fetchrow_args = conn.fetchrow.call_args
    untagged_sql = fetchrow_args.args[0]
    assert "user_library" in untagged_sql, "untagged must join user_library"

    # user_id must be passed as the bind parameter to every call.
    assert fetch_calls[0].args[1] == 42
    assert fetch_calls[1].args[1] == 42
    assert fetchrow_args.args[1] == 42


@pytest.mark.asyncio
async def test_fetch_feed_facet_counts_corpus_scope_omits_user_library():
    """When user_id is None, corpus-scope SQL (no user_library join) is used."""
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [{"source_type": "semantic_scholar", "cnt": 4}],
        [],  # no topics
    ]
    conn.fetchrow.return_value = {"cnt": 1}

    by_source, by_topic, untagged = await fetch_feed_facet_counts(conn, user_id=None)

    assert by_source == {"semantic_scholar": 4}
    assert by_topic == []
    assert untagged == 1

    source_sql = conn.fetch.call_args_list[0].args[0]
    assert "user_library" not in source_sql

    # No positional user_id argument passed when user_id is None.
    assert conn.fetch.call_args_list[0].args == (source_sql,)


@pytest.mark.asyncio
async def test_fetch_feed_facet_counts_empty_library():
    """All facets return empty / zero when the user has no library papers."""
    conn = AsyncMock()
    conn.fetch.side_effect = [[], []]
    conn.fetchrow.return_value = {"cnt": 0}

    by_source, by_topic, untagged = await fetch_feed_facet_counts(conn, user_id=99)

    assert by_source == {}
    assert by_topic == []
    assert untagged == 0


@pytest.mark.asyncio
async def test_fetch_feed_facet_counts_untagged_fetchrow_none():
    """If fetchrow returns None (very unlikely aggregate), untagged defaults to 0."""
    conn = AsyncMock()
    conn.fetch.side_effect = [[], []]
    conn.fetchrow.return_value = None

    _, _, untagged = await fetch_feed_facet_counts(conn, user_id=7)

    assert untagged == 0


# ---------------------------------------------------------------------------
# get_feed_counts router — integration with facets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_feed_counts_includes_facets():
    """Handler response includes by_source, by_topic, and untagged facets."""
    pool, conn = _make_pool_and_conn()

    # 10-bucket fetchrow comes first; untagged fetchrow comes second.
    conn.fetchrow.side_effect = [
        _ten_bucket_row(),
        {"cnt": 3},  # untagged from fetch_feed_facet_counts
    ]
    conn.fetch.side_effect = [
        # by_source
        [{"source_type": "arxiv", "cnt": 8}, {"source_type": "pubmed", "cnt": 2}],
        # by_topic
        [{"topic_id": 2, "name": "NLP", "cnt": 6}, {"topic_id": 3, "name": "RL", "cnt": 4}],
    ]

    result = await papers.get_feed_counts.__wrapped__(_mock_request(), db_pool=pool)

    # Existing 10-bucket fields still present.
    assert result.inbox == 5
    assert result.library == 10
    assert result.all_non_trash == 24

    # New facets.
    assert result.by_source == {"arxiv": 8, "pubmed": 2}
    assert len(result.by_topic) == 2
    assert result.by_topic[0].topic_id == 2
    assert result.by_topic[0].name == "NLP"
    assert result.by_topic[0].count == 6
    assert result.by_topic[1].topic_id == 3
    assert result.by_topic[1].name == "RL"
    assert result.by_topic[1].count == 4
    assert result.untagged == 3


@pytest.mark.asyncio
async def test_get_feed_counts_empty_facets_when_no_library():
    """Handler returns empty facets (not errors) when the user library is empty."""
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.side_effect = [
        {k: 0 for k in _ten_bucket_row()},  # all-zero 10-bucket
        {"cnt": 0},  # untagged
    ]
    conn.fetch.side_effect = [[], []]  # no source rows, no topic rows

    result = await papers.get_feed_counts.__wrapped__(_mock_request(), db_pool=pool)

    assert result.by_source == {}
    assert result.by_topic == []
    assert result.untagged == 0


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_facet_counts_cross_user_isolation():
    """Facet counts for user A must NOT include papers from user B's library.

    Pattern mirrors test_build_feed_queries_user_id_kwarg_threads_into_params
    in test_feed_query.py: we verify the SQL contains the user_library JOIN
    parameterised on the correct user_id, not a different one.
    """
    conn = AsyncMock()

    # User A: 3 arxiv papers, 1 topic.
    conn.fetch.side_effect = [
        [{"source_type": "arxiv", "cnt": 3}],
        [{"topic_id": 10, "name": "CV", "cnt": 3}],
    ]
    conn.fetchrow.return_value = {"cnt": 0}

    by_source_a, by_topic_a, _ = await fetch_feed_facet_counts(conn, user_id=1)

    # Reset for user B who has different data.
    conn.fetch.side_effect = [
        [{"source_type": "openalex", "cnt": 99}],
        [{"topic_id": 10, "name": "CV", "cnt": 50}],
    ]
    conn.fetchrow.return_value = {"cnt": 5}

    by_source_b, by_topic_b, untagged_b = await fetch_feed_facet_counts(conn, user_id=2)

    # User A results are independent from user B results.
    assert by_source_a == {"arxiv": 3}
    assert by_source_b == {"openalex": 99}
    assert by_topic_a[0]["count"] == 3
    assert by_topic_b[0]["count"] == 50
    assert untagged_b == 5

    # Crucially: user_id=1 was passed in the first pair of calls, user_id=2 in the second.
    all_fetch_calls = conn.fetch.call_args_list
    # calls 0-1 → user A, calls 2-3 → user B
    assert all_fetch_calls[0].args[1] == 1  # source call for user A
    assert all_fetch_calls[1].args[1] == 1  # topic call for user A
    assert all_fetch_calls[2].args[1] == 2  # source call for user B
    assert all_fetch_calls[3].args[1] == 2  # topic call for user B


@pytest.mark.asyncio
async def test_feed_facet_counts_untagged_is_user_scoped():
    """The untagged SQL must bind user_id so only the caller's papers are counted."""
    conn = AsyncMock()
    conn.fetch.side_effect = [[], []]
    conn.fetchrow.return_value = {"cnt": 7}

    _, _, untagged = await fetch_feed_facet_counts(conn, user_id=55)

    fetchrow_call = conn.fetchrow.call_args
    sql_used = fetchrow_call.args[0]
    uid_used = fetchrow_call.args[1]

    assert "user_library" in sql_used, "untagged query must scope to user_library"
    assert uid_used == 55
    assert untagged == 7
