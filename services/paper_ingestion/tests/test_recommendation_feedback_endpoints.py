"""Tests for GET and DELETE /api/recommendation_feedback endpoints.

Covers pagination, user-scoping, paper_id filter,
topic-name join, and bulk-delete behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from paper_ingestion.routers import recommendation_feedback
from paper_ingestion.routers._paper_helpers import _upsert_recommendation_feedback

from tests.conftest import _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(**kwargs):
    """Build a minimal asyncpg-style record dict."""
    defaults = {
        "paper_id": 1,
        "title": "Test Paper",
        "signal": "positive",
        "source": "pulse_thumbs",
        "reason": None,
        "topic_id": None,
        "topic_name": None,
        "created_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# GET endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_paginated_list_with_topic_join():
    """Two rows returned → items list of length 2, total == 2."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _row(paper_id=1, title="Paper A", topic_id=10, topic_name="ML"),
        _row(paper_id=2, title="Paper B", topic_id=20, topic_name="NLP"),
    ]
    conn.fetchval.return_value = 2

    result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=None,
        limit=50,
        offset=0,
        db_pool=pool,
        user_id=None,
    )

    assert result.total == 2
    assert len(result.items) == 2
    assert result.items[0].topic_name == "ML"
    assert result.items[1].topic_name == "NLP"


@pytest.mark.asyncio
async def test_get_filters_by_paper_id():
    """When paper_id=42 is supplied, it must appear in the SQL sent to conn.fetch."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_row(paper_id=42)]
    conn.fetchval.return_value = 1

    result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=42,
        limit=50,
        offset=0,
        db_pool=pool,
        user_id=None,
    )

    assert result.total == 1
    # Verify the SQL query passed to conn.fetch includes paper_id as a positional arg.
    # The handler appends paper_id to params when it is not None, so it must appear
    # somewhere in the call_args positional arguments.
    call_args = conn.fetch.call_args
    # call_args.args = (sql_str, *params, limit, offset)
    positional = call_args.args
    assert 42 in positional, f"paper_id=42 not found in fetch args: {positional}"


@pytest.mark.asyncio
async def test_get_scopes_by_user_id():
    """The SQL sent to conn.fetch must scope by an exact user_id match."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_row()]
    conn.fetchval.return_value = 1

    await recommendation_feedback.list_recommendation_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=None,
        limit=50,
        offset=0,
        db_pool=pool,
        user_id=7,
    )

    sql = conn.fetch.call_args.args[0]
    assert "IS NOT DISTINCT FROM" not in sql
    assert "rf.user_id = $1" in sql


@pytest.mark.asyncio
async def test_get_pagination_default():
    """Default limit=50 and offset=0 must be forwarded to the SQL call."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []
    conn.fetchval.return_value = 0

    result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=None,
        limit=50,
        offset=0,
        db_pool=pool,
        user_id=None,
    )

    assert result.total == 0
    assert result.items == []
    # The handler passes limit and offset as the last two positional args after params.
    call_args = conn.fetch.call_args.args
    # Without paper_id: args = (sql, user_id, limit, offset)
    assert call_args[-2] == 50, f"Expected limit=50 but got {call_args[-2]}"
    assert call_args[-1] == 0, f"Expected offset=0 but got {call_args[-1]}"


@pytest.mark.asyncio
async def test_get_topic_name_join():
    """FeedbackListItem is populated with topic_id and topic_name from the join."""
    pool, conn = _make_pool_and_conn()
    now = datetime.now(UTC)
    conn.fetch.return_value = [
        _row(
            paper_id=7,
            title="Physics Paper",
            signal="negative",
            source="feed_thumbs",
            reason="off-topic",
            topic_id=99,
            topic_name="Quantum",
            created_at=now,
        )
    ]
    conn.fetchval.return_value = 1

    result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=None,
        limit=50,
        offset=0,
        db_pool=pool,
        user_id=None,
    )

    item = result.items[0]
    assert item.paper_id == 7
    assert item.title == "Physics Paper"
    assert item.signal == "negative"
    assert item.source == "feed_thumbs"
    assert item.reason == "off-topic"
    assert item.topic_id == 99
    assert item.topic_name == "Quantum"
    assert item.created_at == now


# ---------------------------------------------------------------------------
# DELETE endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_user_scoped_rows_for_topic():
    """DELETE with topic_id=5, mock returns 'DELETE 3' → deleted=3, topic_id=5."""
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 3"

    result = await recommendation_feedback.delete_recommendation_feedback_by_topic.__wrapped__(
        request=MagicMock(),
        topic_id=5,
        db_pool=pool,
        user_id=None,
    )

    assert result.deleted == 3
    assert result.topic_id == 5


@pytest.mark.asyncio
async def test_delete_returns_zero_when_no_rows():
    """DELETE with no matching rows → asyncpg returns 'DELETE 0', response deleted=0."""
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"

    result = await recommendation_feedback.delete_recommendation_feedback_by_topic.__wrapped__(
        request=MagicMock(),
        topic_id=99,
        db_pool=pool,
        user_id=None,
    )

    assert result.deleted == 0
    assert result.topic_id == 99


@pytest.mark.asyncio
async def test_delete_scopes_by_user_id():
    """The DELETE SQL must scope recommendation_feedback by an exact user_id."""
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"

    await recommendation_feedback.delete_recommendation_feedback_by_topic.__wrapped__(
        request=MagicMock(),
        topic_id=1,
        db_pool=pool,
        user_id=7,
    )

    sql = conn.execute.call_args.args[0]
    assert "IS NOT DISTINCT FROM" not in sql
    assert "user_id = $2" in sql


# ---------------------------------------------------------------------------
# _upsert_recommendation_feedback helper — topic_id stamping tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_looks_up_primary_topic_when_topic_id_omitted():
    """When topic_id is not supplied, the helper looks up the highest-relevance
    topic from paper_topics and stamps it on the INSERT."""
    conn = AsyncMock()
    conn.fetchval.return_value = 42  # primary topic from lookup

    await _upsert_recommendation_feedback(
        conn,
        paper_id=10,
        user_id=None,
        signal="negative",
        source="feed_thumbs",
        reason=None,
        # topic_id omitted — helper must perform the lookup
    )

    # fetchval should have been called with the paper_topics lookup SQL + paper_id=10
    conn.fetchval.assert_awaited_once()
    fetchval_sql = conn.fetchval.await_args.args[0]
    assert "paper_topics" in fetchval_sql
    assert "ORDER BY relevance_score DESC NULLS LAST" in fetchval_sql
    assert "LIMIT 1" in fetchval_sql
    assert conn.fetchval.await_args.args[1] == 10  # paper_id param

    # execute should have been called with topic_id=42 as the 6th positional arg
    conn.execute.assert_awaited_once()
    exec_args = conn.execute.await_args.args
    assert "topic_id" in exec_args[0]  # column present in INSERT SQL
    assert exec_args[6] == 42  # $6 = topic_id


@pytest.mark.asyncio
async def test_helper_uses_provided_topic_id_when_given():
    """When topic_id is explicitly supplied, no lookup is performed and the
    provided value is passed directly to the INSERT."""
    conn = AsyncMock()

    await _upsert_recommendation_feedback(
        conn,
        paper_id=20,
        user_id=5,
        signal="positive",
        source="pulse_thumbs",
        reason=None,
        topic_id=99,
    )

    # fetchval must NOT have been called — no lookup needed
    conn.fetchval.assert_not_awaited()

    # execute must have been called with topic_id=99 as the 6th positional arg
    conn.execute.assert_awaited_once()
    exec_args = conn.execute.await_args.args
    assert "topic_id" in exec_args[0]
    assert exec_args[6] == 99  # $6 = topic_id
