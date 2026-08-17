"""Guard tests: assert→RuntimeError replacements in papers_service and papers_lifecycle.

Two production sites replaced:
  1. papers_service.get_feed_counts — aggregate SELECT always returns a row.
  2. routers.papers_lifecycle.annotate_paper — RETURNING always returns a row.

Each test exercises the real code path with a mocked DB so the relevant
fetchrow returns None and verifies that RuntimeError (not
AssertionError/AttributeError) is raised.

Also covers the durable projection boundary: a Learning delivery failure must
not fail the PUT /reading response after its outbox transaction commits.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from jarvis_common.testing import make_pool_and_conn


# ---------------------------------------------------------------------------
# 1. papers_service.get_feed_counts — aggregate SELECT row is None
#
# Shape: boundary-adapter unit test. get_feed_counts is a plain service
# function — called directly with a mocked pool/conn; no route dispatch needed.
# ---------------------------------------------------------------------------


async def test_get_feed_counts_raises_runtime_error_when_fetchrow_returns_none():
    """get_feed_counts must raise RuntimeError when the aggregate fetchrow returns None.

    Verified: services/paper_ingestion/paper_ingestion/papers_service.py:183
    """
    pool, _conn = make_pool_and_conn(fetchrow_return=None)

    # fetch_feed_facet_counts is called after the checked fetchrow; patch it out
    # so we never reach that code path.
    with patch(
        "paper_ingestion.papers_service.fetch_feed_facet_counts",
        AsyncMock(return_value=([], [], 0)),
    ):
        from paper_ingestion.papers_service import get_feed_counts

        with pytest.raises(RuntimeError, match="get_feed_counts"):
            await get_feed_counts(scope="library", db_pool=pool, user_id=1)


async def test_get_feed_counts_threads_active_filters_to_each_count_query():
    """Active source and topic-group filters reach every applicable count query."""
    from paper_ingestion.queries.predicates import paper_untagged_sql

    aggregate_row = {
        "inbox": 1,
        "library": 0,
        "reading_list": 0,
        "reading": 0,
        "done": 0,
        "starred": 0,
        "trash": 0,
        "active": 1,
        "kept": 0,
        "all_non_trash": 1,
    }
    pool, conn = make_pool_and_conn(fetchrow_return=aggregate_row)
    facet_counts = AsyncMock(return_value=({"arxiv": 0}, [], 0))

    with (
        patch(
            "paper_ingestion.papers_service.fetch_feed_facet_counts",
            facet_counts,
        ),
        patch(
            "paper_ingestion.papers_service.paper_untagged_sql",
            wraps=paper_untagged_sql,
        ) as status_untagged_predicate,
    ):
        from paper_ingestion.papers_service import get_feed_counts

        result = await get_feed_counts(
            scope="library",
            db_pool=pool,
            user_id=7,
            view="library",
            source="pubmed",
            topic_id=19,
            untagged=True,
        )

    assert result.library == 0
    assert conn.fetchrow.await_args.args[1:] == (7, "pubmed", 19)
    facet_counts.assert_awaited_once_with(
        conn,
        7,
        scope="library",
        view="library",
        source="pubmed",
        topic_id=19,
        untagged=True,
    )
    status_untagged_predicate.assert_called_once_with()


async def test_untagged_conditions_source_counts_but_not_its_own_group():
    """The active no-topic selection applies once beyond its own bucket query."""
    from paper_ingestion.queries.predicates import paper_untagged_sql
    from paper_ingestion.services.feed_query import fetch_feed_facet_counts

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[[], []])
    conn.fetchrow = AsyncMock(return_value={"cnt": 0})

    with patch(
        "paper_ingestion.services.feed_query.paper_untagged_sql",
        wraps=paper_untagged_sql,
    ) as untagged_predicate:
        result = await fetch_feed_facet_counts(
            conn,
            7,
            scope="library",
            view="library",
            untagged=True,
        )

    assert result == ({}, [], 0)
    assert untagged_predicate.call_count == 2


async def test_feed_counts_route_threads_untagged_selection():
    """The HTTP boundary preserves the active no-topic selection."""
    from jarvis_common.auth import get_current_user_id
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers.papers_feed import router as feed_router

    app = FastAPI()
    app.include_router(feed_router)
    pool, _conn = make_pool_and_conn()
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_current_user_id] = lambda: 7
    service_counts = AsyncMock(
        return_value={
            "inbox": 0,
            "library": 0,
            "reading_list": 0,
            "reading": 0,
            "done": 0,
            "starred": 0,
            "trash": 0,
            "active": 0,
            "kept": 0,
            "all_non_trash": 0,
            "by_source": {},
            "by_topic": [],
            "untagged": 0,
        }
    )

    with patch(
        "paper_ingestion.routers.papers_feed.papers_service.get_feed_counts",
        service_counts,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/papers/feed/counts",
                params={"scope": "library", "untagged": "true"},
            )

    assert response.status_code == 200
    service_counts.assert_awaited_once_with(
        "library",
        pool,
        7,
        view=None,
        source=None,
        topic_id=None,
        untagged=True,
    )


# ---------------------------------------------------------------------------
# 2. routers.papers_lifecycle.annotate_paper — RETURNING row is None
#
# Shape: boundary-adapter ASGI test. The RuntimeError lives inside the
# route handler; the route is exercised through a minimal FastAPI app (no
# exception middleware) so the RuntimeError propagates through httpx with
# raise_app_exceptions=True (default).
#
# Two collaborators are patched at the papers_lifecycle import site:
#   - papers_service.assert_paper_ownership → AsyncMock() (ownership granted)
#   - _upsert_paper_user_state             → AsyncMock(return_value=None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_paper_raises_runtime_error_when_upsert_returns_none():
    """annotate_paper must raise RuntimeError when _upsert_paper_user_state returns None.

    Exercises the real route handler through an ASGI client; patches only the
    two collaborators that would require real DB state (ownership check +
    upsert).  A None return from the upsert must yield RuntimeError, not
    AttributeError/AssertionError.

    Verified: services/paper_ingestion/paper_ingestion/routers/papers_lifecycle.py:323
    """
    from jarvis_common.auth import (
        get_current_user_id,
        get_current_user_id_or_bot,
        verify_api_key,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers.papers_lifecycle import router as lifecycle_router

    # Minimal app — no generic_exception_handler — so RuntimeError propagates.
    app = FastAPI()
    app.include_router(lifecycle_router)

    pool, _conn = make_pool_and_conn()

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: 7
    app.dependency_overrides[get_current_user_id_or_bot] = lambda: 7

    with (
        patch(
            "paper_ingestion.routers.papers_lifecycle.papers_service.assert_paper_ownership",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_ingestion.routers.papers_lifecycle._upsert_paper_user_state",
            AsyncMock(return_value=None),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with pytest.raises(RuntimeError, match="annotate_paper"):
                await client.put(
                    "/api/papers/99/annotations",
                    json={"rating": 3, "user_notes": None, "flagged": None},
                )


# ---------------------------------------------------------------------------
# F10: best-effort — Learning delivery failure must not fail PUT /reading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f10_projection_failure_does_not_fail_reading_mark():
    """PUT /reading succeeds when delivery is pending after durable outbox insertion."""

    from jarvis_common.auth import (
        get_current_user_id,
        get_current_user_id_or_bot,
        verify_api_key,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers.papers_lifecycle import router as lifecycle_router

    app = FastAPI()
    app.include_router(lifecycle_router)
    app.state.http_client = object()

    pool, conn = make_pool_and_conn(fetchval_return="to_read")

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: 7
    app.dependency_overrides[get_current_user_id_or_bot] = lambda: 7

    with (
        patch(
            "paper_ingestion.routers.papers_lifecycle.papers_service.assert_paper_ownership",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_ingestion.routers.papers_lifecycle._assert_paper_in_states",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_ingestion.routers.papers_lifecycle._upsert_state_and_starred",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_ingestion.routers.papers_lifecycle.record_event",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_ingestion.routers.papers_lifecycle.deliver_pending_events",
            AsyncMock(side_effect=RuntimeError("Learning unavailable")),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put("/api/papers/42/reading")

    assert resp.status_code == 200, (
        "PUT /reading must succeed when the durable projection is pending; "
        f"got {resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.asyncio
async def test_projection_retries_a_non_mapping_acknowledgement() -> None:
    """A malformed Learning response remains pending for bounded retry."""
    from paper_ingestion.repos import domain_events

    event_id = uuid.uuid4()
    pool, conn = make_pool_and_conn()
    conn.fetch.return_value = [
        {
            "id": event_id,
            "event_type": "paper.read",
            "user_id": 7,
            "paper_id": 42,
        }
    ]
    response = MagicMock()
    response.json.return_value = []
    client = AsyncMock()
    client.post.return_value = response

    with patch(
        "paper_ingestion.repos.domain_events.authorize_service_command",
        AsyncMock(return_value={"X-Request-Id": str(event_id)}),
    ):
        delivered = await domain_events.deliver_pending_events(
            pool,
            client,
            settings=domain_events.DomainDeliverySettings(
                platform_url="http://platform",
                learning_url="http://learning",
                service_token="test-token",
            ),
        )

    assert delivered == 0
    assert conn.execute.await_args is not None
    assert conn.execute.await_args.args[1] == event_id
