"""Tests for priority scoring logic and the compute_paper_priority endpoint."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from paper_ingestion.models import compute_priority, priority_level

# ---------------------------------------------------------------------------
# compute_priority tests
# ---------------------------------------------------------------------------


def test_compute_priority_all_zeros():
    """All zero inputs produce minimum score."""
    now = datetime.now(UTC)
    score = compute_priority([], None, 0, now)
    # relevance=0, recency=0.5 (default), citation_boost=0
    # 0.5*0 + 0.3*0.5 + 0.2*0 = 0.15
    assert score == 0.15


def test_compute_priority_high_relevance():
    """High relevance score dominates the priority."""
    now = datetime.now(UTC)
    score = compute_priority([0.9, 0.5, 0.3], now, 0, now)
    # relevance=0.9, recency=1.0 (0 days old), citation_boost=0
    # 0.5*0.9 + 0.3*1.0 + 0.2*0 = 0.75
    assert score == 0.75


def test_compute_priority_old_paper():
    """Paper older than 30 days gets zero recency."""
    now = datetime.now(UTC)
    discovered = now - timedelta(days=60)
    score = compute_priority([0.5], discovered, 0, now)
    # relevance=0.5, recency=max(0, 1-60/30)=0.0, citation_boost=0
    # 0.5*0.5 + 0.3*0.0 + 0.2*0 = 0.25
    assert score == 0.25


def test_compute_priority_high_citations():
    """High citation count boosts the score."""
    now = datetime.now(UTC)
    score = compute_priority([], None, 200, now)
    # relevance=0, recency=0.5, citation_boost=min(1.0, 200/100)=1.0
    # 0.5*0 + 0.3*0.5 + 0.2*1.0 = 0.35
    assert score == 0.35


def test_compute_priority_perfect_score():
    """Perfect inputs produce maximum score."""
    now = datetime.now(UTC)
    score = compute_priority([1.0], now, 100, now)
    # relevance=1.0, recency=1.0, citation_boost=1.0
    # 0.5*1.0 + 0.3*1.0 + 0.2*1.0 = 1.0
    assert score == 1.0


def test_compute_priority_moderate_case():
    """Moderate inputs produce a mid-range score."""
    now = datetime.now(UTC)
    discovered = now - timedelta(days=15)
    score = compute_priority([0.6], discovered, 50, now)
    # relevance=0.6, recency=max(0, 1-15/30)=0.5, citation_boost=50/100=0.5
    # 0.5*0.6 + 0.3*0.5 + 0.2*0.5 = 0.55
    assert score == 0.55


def test_compute_priority_none_citation_count():
    """None citation_count treated as zero."""
    now = datetime.now(UTC)
    score = compute_priority([0.5], now, None, now)
    # relevance=0.5, recency=1.0, citation_boost=0
    # 0.5*0.5 + 0.3*1.0 + 0.2*0 = 0.55
    assert score == 0.55


def test_compute_priority_empty_relevance_scores():
    """Empty relevance_scores list uses 0.0 relevance."""
    now = datetime.now(UTC)
    score = compute_priority([], now, 50, now)
    # relevance=0, recency=1.0, citation_boost=0.5
    # 0.5*0 + 0.3*1.0 + 0.2*0.5 = 0.4
    assert score == 0.4


def test_compute_priority_uses_max_relevance():
    """Multiple relevance scores -- max is used."""
    now = datetime.now(UTC)
    score_multi = compute_priority([0.3, 0.8, 0.5], now, 0, now)
    score_single = compute_priority([0.8], now, 0, now)
    assert score_multi == score_single


# ---------------------------------------------------------------------------
# priority_level tests
# ---------------------------------------------------------------------------


def test_priority_level_none():
    """None score returns 'unscored'."""
    assert priority_level(None) == "unscored"


def test_priority_level_must_read():
    """Score > 0.7 is must-read."""
    assert priority_level(0.71) == "must-read"
    assert priority_level(0.9) == "must-read"
    assert priority_level(1.0) == "must-read"


def test_priority_level_recommended():
    """Score 0.4 < score <= 0.7 is recommended."""
    assert priority_level(0.41) == "recommended"
    assert priority_level(0.55) == "recommended"
    assert priority_level(0.7) == "recommended"


def test_priority_level_background():
    """Score <= 0.4 is background."""
    assert priority_level(0.0) == "background"
    assert priority_level(0.2) == "background"
    assert priority_level(0.4) == "background"


def test_priority_level_boundary_0_7():
    """Boundary at 0.7 is recommended, not must-read."""
    assert priority_level(0.7) == "recommended"
    assert priority_level(0.7001) == "must-read"


def test_priority_level_boundary_0_4():
    """Boundary at 0.4 is background, not recommended."""
    assert priority_level(0.4) == "background"
    assert priority_level(0.4001) == "recommended"


# ---------------------------------------------------------------------------
# compute_paper_priority endpoint — ownership (IDOR) tests
# ---------------------------------------------------------------------------


def _make_priority_client(user_id_override=None):
    """Return (TestClient, pool, conn, app) with the priority router mounted.

    Parameters
    ----------
    user_id_override:
        Value that ``current_user_id_or_none`` will return.
        None means single-user mode.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jarvis_common import current_user_id_or_none, verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.routers import priority as priority_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    app.state.db_pool = pool

    app.include_router(priority_router.router)

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_or_none] = lambda: user_id_override

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, pool, conn, app


def test_compute_paper_priority_rejects_unowned_paper():
    """POST /api/papers/{id}/priority returns 403 when user B tries a paper owned by user A.

    Wave-1 IDOR fix: assert_paper_ownership must be called before any DB read/write.
    When it raises HTTPException(403) the endpoint must propagate that status unchanged.
    """
    tc, pool, conn, app = _make_priority_client(user_id_override=None)

    try:
        with (
            patch(
                "paper_ingestion.routers.priority.current_user_id_or_none",
                new=AsyncMock(return_value=2),
            ),
            patch(
                "paper_ingestion.routers.priority.assert_paper_ownership",
                new=AsyncMock(
                    side_effect=HTTPException(
                        status_code=403, detail="paper not owned by current user"
                    )
                ),
            ) as mock_ownership,
        ):
            resp = tc.post("/api/papers/99/priority")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 403, (
        f"Expected 403 (IDOR guard), got {resp.status_code}: {resp.text}"
    )
    # Ownership check must have been called with the paper_id from the URL
    mock_ownership.assert_awaited_once()
    _conn_arg, paper_id_arg, user_id_arg = mock_ownership.await_args.args
    assert paper_id_arg == 99
    assert user_id_arg == 2
    # No DB read/write should occur after the ownership rejection
    conn.fetchrow.assert_not_called()
    conn.execute.assert_not_called()


def test_compute_paper_priority_passes_for_owner():
    """POST /api/papers/{id}/priority returns 200 when ownership check passes.

    Verifies that assert_paper_ownership is called and that successful
    ownership verification allows the endpoint to complete normally.
    """
    from tests.conftest import FakeRecord

    tc, pool, conn, app = _make_priority_client(user_id_override=None)

    now = datetime.now(UTC)
    conn.fetchrow.return_value = FakeRecord(
        {
            "id": 42,
            "discovered_at": now,
            "citation_count": 10,
        }
    )
    conn.fetch.return_value = [FakeRecord({"relevance_score": 0.8})]
    conn.execute.return_value = "UPDATE 1"

    try:
        with (
            patch(
                "paper_ingestion.routers.priority.current_user_id_or_none",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "paper_ingestion.routers.priority.assert_paper_ownership",
                new=AsyncMock(return_value=None),
            ) as mock_ownership,
        ):
            resp = tc.post("/api/papers/42/priority")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["paper_id"] == 42
    assert "priority_score" in body
    assert "priority_level" in body
    # assert_paper_ownership was called with the correct paper_id and user_id
    mock_ownership.assert_awaited_once()
    _conn_arg, paper_id_arg, user_id_arg = mock_ownership.await_args.args
    assert paper_id_arg == 42
    assert user_id_arg == 1


def test_compute_paper_priority_single_user_mode_skips_ownership():
    """POST /api/papers/{id}/priority succeeds with user_id=None (single-user mode).

    assert_paper_ownership short-circuits when user_id is None — ownership
    check is bypassed but still called.
    """
    from tests.conftest import FakeRecord

    tc, pool, conn, app = _make_priority_client(user_id_override=None)

    now = datetime.now(UTC)
    conn.fetchrow.return_value = FakeRecord(
        {
            "id": 7,
            "discovered_at": now,
            "citation_count": 0,
        }
    )
    conn.fetch.return_value = []
    conn.execute.return_value = "UPDATE 1"

    try:
        with (
            patch(
                "paper_ingestion.routers.priority.current_user_id_or_none",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "paper_ingestion.routers.priority.assert_paper_ownership",
                new=AsyncMock(return_value=None),
            ) as mock_ownership,
        ):
            resp = tc.post("/api/papers/7/priority")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    # Ownership called with user_id=None
    mock_ownership.assert_awaited_once()
    _conn_arg, paper_id_arg, user_id_arg = mock_ownership.await_args.args
    assert paper_id_arg == 7
    assert user_id_arg is None


# ---------------------------------------------------------------------------
# recompute_all_priorities — admin gate tests (3.17)
# ---------------------------------------------------------------------------


def _make_recompute_client(*, user_role: str | None):
    """Return (TestClient, conn, app) with the priority router mounted.

    Parameters
    ----------
    user_role:
        Value placed on ``request.state.user_role``.  ``None`` simulates an
        API-key-only caller (no session cookie — legacy single-tenant path,
        allowed through by ``require_admin``).
        ``"user"`` simulates a non-admin browser session → must get 403.
        ``"admin"`` simulates an admin browser session → must get 200.
    """
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.routers import priority as priority_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    # conn.fetch returns empty list so endpoint completes quickly
    conn.fetch.return_value = []

    app.include_router(priority_router.router)
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None

    # Inject user_role into request.state by overriding require_admin to a
    # version that sets state first, then delegates to the real dependency.
    async def _patched_require_admin(request: Request) -> None:
        if user_role is not None:
            request.state.user_role = user_role
        await require_admin(request)

    app.dependency_overrides[require_admin] = _patched_require_admin

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, conn, app


def test_recompute_all_priorities_rejects_non_admin():
    """POST /api/papers/recompute-priorities returns 403 for a non-admin caller.

    3.17 fix: require_admin dependency must be declared on the route so that
    any browser session with role != 'admin' is rejected before any DB work.
    """
    tc, conn, app = _make_recompute_client(user_role="user")
    try:
        resp = tc.post("/api/papers/recompute-priorities")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 403, (
        f"Expected 403 (admin gate), got {resp.status_code}: {resp.text}"
    )
    # No DB work should be done after the rejection
    conn.fetch.assert_not_called()
    conn.executemany.assert_not_called()


def test_recompute_all_priorities_accepts_admin():
    """POST /api/papers/recompute-priorities returns 200 for an admin caller.

    3.17 fix: admin-role browser sessions (and API-key-only callers with no
    session cookie) must be allowed through.
    """
    tc, conn, app = _make_recompute_client(user_role="admin")
    try:
        resp = tc.post("/api/papers/recompute-priorities")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body == {"updated": 0}
