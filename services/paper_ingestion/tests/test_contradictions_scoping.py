"""RB-3: Auth enforcement + cross-tenant scoping for GET /api/contradictions.

(a) No session → 401.
(b) User B does not receive user A's contradictions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_pool() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


def _contradiction_row(*, paper_a_id: int = 10, paper_b_id: int = 11) -> FakeRecord:
    return FakeRecord(
        {
            "id": 1,
            "paper_a_id": paper_a_id,
            "paper_b_id": paper_b_id,
            "paper_a_title": "Paper A",
            "paper_b_title": "Paper B",
            "finding_a": "A",
            "finding_b": "B",
            "quote_a": "quote A",
            "quote_b": "quote B",
            "page_a": 1,
            "page_b": 2,
            "contradiction_type": "result",
            "explanation": "Conflict",
            "confidence": 0.85,
            "status": "verified",
            "created_at": datetime.now(UTC),
            "total_count": 1,
        }
    )


# ---------------------------------------------------------------------------
# (a) No session → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.real_auth
async def test_get_contradictions_no_session_returns_401() -> None:
    """GET /api/contradictions without a session must return 401."""
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, _conn = _mock_pool()
    app.state.db_pool = pool
    app.state.limiter.enabled = False

    async def override_db_pool():
        return pool

    async def override_api_key():
        return None

    app.dependency_overrides[get_db_pool] = override_db_pool
    app.dependency_overrides[verify_api_key] = override_api_key
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/contradictions")
        assert resp.status_code == 401, resp.text
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# (b) Cross-tenant isolation: user B does not see user A's rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_contradictions_cross_tenant_isolation() -> None:
    """User B must not receive contradictions that belong only to user A.

    The SQL predicate scopes via user_library: only rows where at least one
    paper side is in the caller's library are returned. This test verifies
    that user_id=2 (user B) gets an empty result when the DB returns nothing
    (simulating that neither paper_a nor paper_b is in user B's library).
    """
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _mock_pool()
    # DB returns empty — user B has no contradictions in their library.
    conn.fetch.return_value = []

    app.state.db_pool = pool
    app.state.limiter.enabled = False

    async def override_db_pool():
        return pool

    async def override_api_key():
        return None

    app.dependency_overrides[get_db_pool] = override_db_pool
    app.dependency_overrides[verify_api_key] = override_api_key

    # Patch current_user_id_strict in the contradictions router to return user_id=2.
    with patch(
        "paper_ingestion.routers.contradictions.current_user_id_strict",
        new=AsyncMock(return_value=2),
    ):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/contradictions")
        finally:
            app.dependency_overrides.clear()
            app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["contradictions"] == []

    # Confirm user_id=2 was forwarded to the DB query (first positional param).
    call_args = conn.fetch.await_args
    assert call_args is not None
    query_params = call_args.args[1:]  # first arg is SQL string
    assert 2 in query_params, f"user_id=2 not found in DB params: {query_params}"


@pytest.mark.asyncio
async def test_get_contradictions_user_id_threaded_to_sql() -> None:
    """Verify the user_id is included in the SQL query params."""
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _mock_pool()
    conn.fetch.return_value = [_contradiction_row()]

    app.state.db_pool = pool
    app.state.limiter.enabled = False

    async def override_db_pool():
        return pool

    async def override_api_key():
        return None

    app.dependency_overrides[get_db_pool] = override_db_pool
    app.dependency_overrides[verify_api_key] = override_api_key

    # The autofixture patches current_user_id_strict to return 1 (user A).
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/contradictions")
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    # user_id=1 must appear in the SQL params forwarded by list_contradictions.
    call_args = conn.fetch.await_args
    assert call_args is not None
    query_params = call_args.args[1:]
    assert 1 in query_params, f"user_id=1 not found in DB params: {query_params}"
    # Also confirm user_library scoping clause is present in the SQL.
    sql = call_args.args[0]
    assert "user_library" in sql, "user_library JOIN/subquery missing from generated SQL"
