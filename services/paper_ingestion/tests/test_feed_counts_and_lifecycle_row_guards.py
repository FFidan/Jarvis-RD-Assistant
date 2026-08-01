"""Guard tests: assert→RuntimeError replacements in papers_service and papers_lifecycle.

Two production sites replaced:
  1. papers_service.get_feed_counts — aggregate SELECT always returns a row.
  2. routers.papers_lifecycle.annotate_paper — RETURNING always returns a row.

Each test exercises the real code path with a mocked DB so the relevant
fetchrow returns None and verifies that RuntimeError (not
AssertionError/AttributeError) is raised.

Also covers F10 best-effort behaviour: a daily_log write failure must not fail
the PUT /reading response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
# F10: best-effort — daily_log write failure must not fail PUT /reading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f10_daily_log_failure_does_not_fail_reading_mark():
    """PUT /reading returns 200 even when the daily_log upsert raises.

    Patches: ownership check → granted; assert_paper_in_states → allowed;
    conn.fetchval → 'to_read' (state_before); _upsert_state_and_starred → no-op;
    conn.execute → raises asyncpg.PostgresError (simulates daily_log failure).

    Verified: papers_lifecycle.py — the daily_log execute is wrapped in try/except.
    """
    import asyncpg

    from jarvis_common.auth import (
        get_current_user_id,
        get_current_user_id_or_bot,
        verify_api_key,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers.papers_lifecycle import router as lifecycle_router

    app = FastAPI()
    app.include_router(lifecycle_router)

    pool, conn = make_pool_and_conn(fetchval_return="to_read")
    conn.execute = AsyncMock(side_effect=asyncpg.PostgresError("simulated daily_log failure"))

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
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put("/api/papers/42/reading")

    assert resp.status_code == 200, (
        f"PUT /reading must succeed even when daily_log write fails; got {resp.status_code}: {resp.text[:200]}"
    )
