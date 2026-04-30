"""Tests for GET and DELETE /api/recommendation_feedback endpoints.

Wave 1cd Task C8 — covers pagination, user-scoping, paper_id filter,
topic-name join, and bulk-delete behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.routers import recommendation_feedback

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Mirror the helper in test_papers_lifecycle.py."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


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

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
            request=MagicMock(),
            paper_id=None,
            limit=50,
            offset=0,
            db_pool=pool,
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

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
            request=MagicMock(),
            paper_id=42,
            limit=50,
            offset=0,
            db_pool=pool,
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
    """The SQL sent to conn.fetch must contain the IS NOT DISTINCT FROM clause."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_row()]
    conn.fetchval.return_value = 1

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        await recommendation_feedback.list_recommendation_feedback.__wrapped__(
            request=MagicMock(),
            paper_id=None,
            limit=50,
            offset=0,
            db_pool=pool,
        )

    sql = conn.fetch.call_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql


@pytest.mark.asyncio
async def test_get_pagination_default():
    """Default limit=50 and offset=0 must be forwarded to the SQL call."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []
    conn.fetchval.return_value = 0

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
            request=MagicMock(),
            paper_id=None,
            limit=50,
            offset=0,
            db_pool=pool,
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

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        result = await recommendation_feedback.list_recommendation_feedback.__wrapped__(
            request=MagicMock(),
            paper_id=None,
            limit=50,
            offset=0,
            db_pool=pool,
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

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        result = await recommendation_feedback.delete_recommendation_feedback_by_topic.__wrapped__(
            request=MagicMock(),
            topic_id=5,
            db_pool=pool,
        )

    assert result.deleted == 3
    assert result.topic_id == 5


@pytest.mark.asyncio
async def test_delete_returns_zero_when_no_rows():
    """DELETE with no matching rows → asyncpg returns 'DELETE 0', response deleted=0."""
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        result = await recommendation_feedback.delete_recommendation_feedback_by_topic.__wrapped__(
            request=MagicMock(),
            topic_id=99,
            db_pool=pool,
        )

    assert result.deleted == 0
    assert result.topic_id == 99


@pytest.mark.asyncio
async def test_delete_scopes_by_user_id():
    """The SQL passed to conn.execute must include the user_id IS NOT DISTINCT FROM clause."""
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"

    with patch(
        "paper_ingestion.routers.recommendation_feedback.current_user_id_or_none",
        new=AsyncMock(return_value=None),
    ):
        await recommendation_feedback.delete_recommendation_feedback_by_topic.__wrapped__(
            request=MagicMock(),
            topic_id=1,
            db_pool=pool,
        )

    sql = conn.execute.call_args.args[0]
    assert "user_id IS NOT DISTINCT FROM" in sql
