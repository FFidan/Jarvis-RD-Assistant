"""Durability of the Research outbox: retry ceiling, requeue, and per-owner delivery.

Three properties the projection seam depends on:

1. A delivery that keeps failing is retried for long enough that a planned
   database outage cannot strand it permanently.
2. An event that was stranded anyway can be put back on the queue, because an
   undelivered ``paper.deleted`` keeps a deleted paper's private rows retained.
3. Marking a paper as reading delivers that reader's own work only — never
   another user's, at that user's expense, inside a request handler.

The outbox is exercised through mocked connections in the same shape as the
other repository tests: each case invokes the real function and asserts on the
statement and the values it binds.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from jarvis_common.testing import make_pool_and_conn

from paper_ingestion.repos.domain_events import (
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_CEILING,
    DomainDeliverySettings,
    _mark_failure,
    deliver_pending_events,
    requeue_dead_lettered_events,
)

_SETTINGS = DomainDeliverySettings(
    platform_url="http://platform",
    learning_url="http://learning",
    service_token="test-token",
)

# An operator taking the database down for an unhurried evening of maintenance.
_MAINTENANCE_WINDOW_SECONDS = 4 * 60 * 60

_INTERVAL_UNIT_SECONDS = {
    "second": 1,
    "seconds": 1,
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
}


def _interval_seconds(literal: str) -> int:
    """Convert an SQL interval literal such as ``6 hours`` into seconds."""
    amount, unit = literal.split()
    return int(amount) * _INTERVAL_UNIT_SECONDS[unit]


@pytest.mark.asyncio
async def test_delivery_retries_outlive_a_maintenance_window() -> None:
    """The backoff series must span longer than a planned outage before dead-lettering.

    Dead-lettering is terminal for the projection, so a ceiling shorter than a
    maintenance window converts a planned outage into permanent retention.
    """
    ceiling = _interval_seconds(RETRY_BACKOFF_CEILING)
    # The SQL computes the delay from the pre-increment attempt count, so the
    # attempts that precede the dead-lettering one are 0 .. MAX - 2.
    span = sum(
        min(ceiling, RETRY_BACKOFF_BASE_SECONDS * 2**attempt)
        for attempt in range(MAX_DELIVERY_ATTEMPTS - 1)
    )
    assert span > _MAINTENANCE_WINDOW_SECONDS, (
        f"retries span {span}s before dead-lettering, which a "
        f"{_MAINTENANCE_WINDOW_SECONDS}s maintenance window exhausts"
    )

    pool, conn = make_pool_and_conn()
    await _mark_failure(pool, uuid.uuid4())

    statement = conn.execute.await_args.args[0]
    assert f"INTERVAL '{RETRY_BACKOFF_CEILING}'" in statement
    assert f">= {MAX_DELIVERY_ATTEMPTS}" in statement


@pytest.mark.asyncio
async def test_a_dead_lettered_event_can_be_requeued() -> None:
    """Requeueing clears the dead-letter mark and reports how many events moved."""
    pool, conn = make_pool_and_conn(fetch_return=[{"id": uuid.uuid4()}, {"id": uuid.uuid4()}])

    moved = await requeue_dead_lettered_events(pool, user_id=7)

    assert moved == 2
    statement, bound_user = conn.fetch.await_args.args
    assert bound_user == 7
    assert "SET dead_lettered_at = NULL" in statement
    assert "stalled.dead_lettered_at IS NOT NULL" in statement
    assert "stalled.delivered_at IS NULL" in statement


@pytest.mark.asyncio
async def test_pending_delivery_can_be_scoped_to_one_owner() -> None:
    """A scoped drain binds the owner and asks the database to filter on it."""
    pool, conn = make_pool_and_conn(fetch_return=[])

    delivered = await deliver_pending_events(
        pool, AsyncMock(), settings=_SETTINGS, limit=1, user_id=7
    )

    assert delivered == 0
    statement, bound_limit, bound_user = conn.fetch.await_args.args
    assert (bound_limit, bound_user) == (1, 7)
    assert "user_id = $2" in statement


@pytest.mark.asyncio
async def test_marking_a_paper_as_reading_delivers_only_that_reader_s_events() -> None:
    """PUT /reading must not spend a reader's request delivering other users' events."""
    from jarvis_common.auth import get_current_user_id, verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers.papers_lifecycle import router as lifecycle_router

    app = FastAPI()
    app.include_router(lifecycle_router)
    app.state.http_client = object()

    pool, _conn = make_pool_and_conn(fetchval_return="to_read")
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: 7

    settings = MagicMock()
    settings.research_service_token_file.read_text.return_value = "test-token\n"
    settings.platform_api_url = "http://platform"
    settings.learning_engine_url = "http://learning"
    deliver = AsyncMock(return_value=1)

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
            AsyncMock(return_value=uuid.uuid4()),
        ),
        patch(
            "paper_ingestion.routers.papers_lifecycle.get_paper_ingestion_settings",
            return_value=settings,
        ),
        patch("paper_ingestion.routers.papers_lifecycle.deliver_pending_events", deliver),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put("/api/papers/42/reading")

    assert response.status_code == 200, response.text[:200]
    assert deliver.await_args.kwargs["user_id"] == 7
    assert deliver.await_args.kwargs["limit"] == 1
