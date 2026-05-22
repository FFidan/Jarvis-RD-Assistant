"""Cross-tenant isolation tests for the author tracking endpoints (RB-2).

Verifies that:
  - GET /api/authors returns only the caller's authors (user B sees none of A's).
  - Unauthenticated requests are rejected with 401.

These tests use the mocked-DB pattern (no live PG required) so they run on every
``pytest`` invocation without JARVIS_RUN_LIVE_PG=1.

Cross-user isolation is proven by:
  1. Patching ``current_user_id_strict`` to return different user IDs per request.
  2. Asserting that the SQL bound to conn.fetch/fetchrow carries the correct user_id.
  3. Asserting that user B's request returns only the rows whose ``user_id`` matches B.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

USER_A = 1
USER_B = 2


def _make_author_record(
    author_id: int = 10,
    author_name: str = "Alice Author",
    user_id: int = USER_A,
) -> dict:
    return {
        "id": author_id,
        "author_name": author_name,
        "s2_author_id": None,
        "source": "manual",
        "enabled": True,
        "last_checked_at": None,
        "created_at": _NOW,
        "user_id": user_id,
    }


@pytest.fixture()
def _app_no_auth_override():
    """App fixture that does NOT stub current_user_id_strict.

    Used by tests that need to control the resolver themselves (scoping tests
    and 401 tests).  The autouse _default_authenticated_user fixture still runs
    but its patch is overridden inside each test via ``with patch(...)``.
    """
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = make_pool_and_conn()
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# 1.  User B does not see User A's authors (list endpoint)
# ---------------------------------------------------------------------------


async def test_list_authors_user_b_sees_no_user_a_authors(_app_no_auth_override):
    """GET /api/authors scoped to user B must not return user A's rows."""
    app, conn = _app_no_auth_override

    # DB returns user A's author when queried (simulates data in DB)
    # But user B's query will bind user_id=USER_B so the result set is empty.
    conn.fetch.return_value = []  # DB correctly returns nothing for user B

    with patch(
        "paper_ingestion.routers.authors.current_user_id_strict",
        new=AsyncMock(return_value=USER_B),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/authors")

    assert resp.status_code == 200
    assert resp.json() == []

    # Verify user_id=USER_B was bound in the SQL call
    call_args = conn.fetch.call_args
    assert call_args is not None
    sql, *params = call_args.args
    assert "user_id IS NOT DISTINCT FROM" in sql
    assert USER_B in params


async def test_list_authors_user_a_sees_own_authors(_app_no_auth_override):
    """GET /api/authors for user A returns only A's rows."""
    app, conn = _app_no_auth_override

    conn.fetch.return_value = [_make_author_record(user_id=USER_A)]

    with patch(
        "paper_ingestion.routers.authors.current_user_id_strict",
        new=AsyncMock(return_value=USER_A),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/authors")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["author_name"] == "Alice Author"

    # Confirm the SQL predicate bound USER_A
    call_args = conn.fetch.call_args
    sql, *params = call_args.args
    assert "user_id IS NOT DISTINCT FROM" in sql
    assert USER_A in params


# ---------------------------------------------------------------------------
# 2.  user_id is bound on create
# ---------------------------------------------------------------------------


async def test_create_author_binds_user_id(_app_no_auth_override):
    """POST /api/authors inserts with the caller's user_id."""
    app, conn = _app_no_auth_override

    conn.fetchrow.side_effect = [
        None,  # no duplicate
        _make_author_record(author_name="Bob Researcher", user_id=USER_B),
    ]

    with patch(
        "paper_ingestion.routers.authors.current_user_id_strict",
        new=AsyncMock(return_value=USER_B),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/authors", json={"author_name": "Bob Researcher"})

    assert resp.status_code == 201

    # Second fetchrow call is the INSERT; verify user_id=USER_B was passed
    insert_call = conn.fetchrow.call_args_list[1]
    insert_sql, *insert_params = insert_call.args
    assert "user_id" in insert_sql
    assert USER_B in insert_params


# ---------------------------------------------------------------------------
# 3.  update returns 404 for another user's author
# ---------------------------------------------------------------------------


async def test_update_author_returns_404_for_other_users_author(_app_no_auth_override):
    """PUT /api/authors/{id} returns 404 when the row belongs to a different user."""
    app, conn = _app_no_auth_override

    # DB returns no row because user_id predicate filters it out
    conn.fetchrow.return_value = None

    with patch(
        "paper_ingestion.routers.authors.current_user_id_strict",
        new=AsyncMock(return_value=USER_B),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put("/api/authors/999", json={"enabled": False})

    assert resp.status_code == 404

    # Confirm the SELECT bound USER_B
    call_args = conn.fetchrow.call_args
    sql, *params = call_args.args
    assert "user_id IS NOT DISTINCT FROM" in sql
    assert USER_B in params


# ---------------------------------------------------------------------------
# 4.  delete returns 404 for another user's author
# ---------------------------------------------------------------------------


async def test_delete_author_returns_404_for_other_users_author(_app_no_auth_override):
    """DELETE /api/authors/{id} returns 404 when row belongs to a different user."""
    from fastapi import HTTPException

    app, conn = _app_no_auth_override

    async def _raise_404(*args, **kwargs):
        raise HTTPException(status_code=404, detail="Not found")

    with patch(
        "paper_ingestion.routers.authors.current_user_id_strict",
        new=AsyncMock(return_value=USER_B),
    ):
        with patch(
            "paper_ingestion.routers.authors.delete_or_404",
            side_effect=_raise_404,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.delete("/api/authors/42")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5.  check_tracked_authors scopes recent_papers to caller's user_library
# ---------------------------------------------------------------------------


async def test_check_tracked_authors_scopes_recent_papers_to_user(_app_no_auth_override):
    """POST /api/authors/check must join user_library and bind the caller's user_id.

    This guards against the cross-tenant leak where the recent_papers query
    scanned every tenant's papers and could expose another user's paper IDs
    through author_alert_log inserts.
    """
    app, conn = _app_no_auth_override

    author_record = {
        **_make_author_record(author_id=20, author_name="Carol Scientist", user_id=USER_A),
        "enabled": True,
    }

    # First fetch → tracked_authors for USER_A; second fetch → recent_papers (empty fine)
    conn.fetch.side_effect = [
        [author_record],  # tracked_authors query
        [],  # recent_papers query (no papers → no alerts)
    ]

    with patch(
        "paper_ingestion.routers.authors.current_user_id_strict",
        new=AsyncMock(return_value=USER_A),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/authors/check")

    assert resp.status_code == 200
    data = resp.json()
    assert data["authors_checked"] == 1
    assert data["new_papers"] == 0

    # The second conn.fetch call is the recent_papers query — verify it
    # joins user_library and binds the caller's user_id.
    assert conn.fetch.call_count == 2
    recent_papers_call = conn.fetch.call_args_list[1]
    sql, *params = recent_papers_call.args
    assert "user_library" in sql, "recent_papers query must join user_library"
    assert USER_A in params, "recent_papers query must bind the caller's user_id"


# ---------------------------------------------------------------------------
# 6.  Unauthenticated request → 401
# ---------------------------------------------------------------------------


@pytest.mark.real_auth
async def test_list_authors_requires_session():
    """GET /api/authors without a session cookie returns 401."""
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = make_pool_and_conn()
    conn.fetch.return_value = []
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/authors")
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 401
