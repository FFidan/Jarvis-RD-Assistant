"""Settings-router analytics contract tests — papers-by-status de-dup (DDB-3).

Covers GET /api/analytics/papers-by-status (settings.py:288 papers_by_status →
analytics_queries.fetch_papers_by_status). The admin (unscoped) branch must use
COUNT(DISTINCT p.id) so a paper with multiple paper_user_state rows in the same
status is counted ONCE, not once per state row.

Survivor-of:
  - services/paper_ingestion/tests/test_analytics_queries.py (deleted):
    SQL-substring mock-unit (TS-02 violation) that asserted "COUNT(DISTINCT p.id)"
    against an AsyncMock and could not prove the de-dup behavior against real rows.

Idiomatic-mock carve-out (KEEP):
  - app.state.http_client = MagicMock() (outbound HTTP boundary)
  - app.state.embedder = MagicMock() (Ollama/Qdrant boundary)
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool
from jarvis_common.testing_contract_apps import make_contract_client as _client

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# App + client fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app(contract_conn):
    """paper_ingestion app wired to the contract connection, rate limiter off."""
    from unittest.mock import MagicMock

    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_embedder = getattr(app.state, "embedder", None)

    app.state.db_pool = shared
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()
    app.dependency_overrides[get_db_pool] = lambda: shared

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False

    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
        if original_pool is None:
            app.state.__dict__.pop("db_pool", None)
        else:
            app.state.db_pool = original_pool
        if original_http is None:
            app.state.__dict__.pop("http_client", None)
        else:
            app.state.http_client = original_http
        if original_embedder is None:
            app.state.__dict__.pop("embedder", None)
        else:
            app.state.embedder = original_embedder
        app.dependency_overrides.pop(get_db_pool, None)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_user_with_session(conn, email: str, role: str) -> tuple[int, str]:
    """Insert one user with *role* + a valid session; return (user_id, cookie)."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, $2) RETURNING id",
        email,
        role,
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day') RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


async def _seed_paper(conn, external_id: str, discovered_by: int | None) -> int:
    """Insert one paper; return its id."""
    return int(
        await conn.fetchval(
            """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
               VALUES ($1, 'arxiv', 'Analytics paper', ARRAY['A. Author'],
                       'https://example.test/analytics', $2)
               RETURNING id""",
            external_id,
            discovered_by,
        )
    )


async def _set_state(conn, paper_id: int, user_id: int, state: str) -> None:
    """Insert a paper_user_state row pinning *paper_id*/*user_id* to *state*."""
    await conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, $2, $3)",
        paper_id,
        user_id,
        state,
    )


# ---------------------------------------------------------------------------
# §A-ANALYTICS-01 — admin papers-by-status de-dups multi-state papers
# Verified: services/paper_ingestion/paper_ingestion/services/analytics_queries.py:47
# (admin branch COUNT(DISTINCT p.id) over LEFT JOIN paper_user_state)
# ---------------------------------------------------------------------------


async def test_admin_papers_by_status_dedups_multi_state_paper(
    contract_conn, _pi_app, _configure_api_key
):
    """A single paper with two paper_user_state rows in the same status counts as 1.

    Seed ONE paper plus two users who each pin that paper to state='reading'. The
    admin (unscoped) LEFT JOIN on paper_user_state yields two rows for that paper,
    so COUNT(*) would report reading=2. COUNT(DISTINCT p.id) collapses them to 1.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:288
    # (papers_by_status → fetch_papers_by_status with is_admin from request.state).
    """
    admin_id, admin_cookie = await _seed_user_with_session(
        contract_conn, "analytics-admin@contract.example.com", "admin"
    )
    other_id, _ = await _seed_user_with_session(
        contract_conn, "analytics-other@contract.example.com", "user"
    )

    paper_id = await _seed_paper(contract_conn, "analytics-dedup-1", admin_id)
    await _set_state(contract_conn, paper_id, admin_id, "reading")
    await _set_state(contract_conn, paper_id, other_id, "reading")

    async with _client(_pi_app, admin_cookie) as c:
        resp = await c.get("/api/analytics/papers-by-status")

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text}"
    counts = {row["status"]: row["count"] for row in resp.json()}
    assert counts.get("reading") == 1, (
        "Admin papers-by-status must de-dup a paper with two same-status rows: "
        f"expected reading=1 (COUNT DISTINCT p.id), got {counts.get('reading')!r}; "
        "COUNT(*) over the LEFT JOIN would report 2."
    )


# ---------------------------------------------------------------------------
# §A-ANALYTICS-01b — admin papers-by-status counts each paper once across buckets
# Verified: services/paper_ingestion/paper_ingestion/services/analytics_queries.py:44
# (admin branch must not double-count a paper that appears in multiple buckets)
# ---------------------------------------------------------------------------


async def test_admin_papers_by_status_counts_multi_state_paper_once(
    contract_conn, _pi_app, _configure_api_key
):
    """A paper assigned to two different states by two users counts once in total.

    Seed ONE paper with user A in state='reading' and user B in state='done'. The
    admin (unscoped) LEFT JOIN on paper_user_state yields two rows for that paper —
    one per status. Without a DISTINCT ON collapse, the paper lands in both the
    'reading' AND 'done' buckets, so sum(counts) == 2 despite only 1 paper existing.
    After the fix, sum(counts) must equal 1.
    """
    admin_id, admin_cookie = await _seed_user_with_session(
        contract_conn, "analytics-cross-admin@contract.example.com", "admin"
    )
    other_id, _ = await _seed_user_with_session(
        contract_conn, "analytics-cross-other@contract.example.com", "user"
    )

    paper_id = await _seed_paper(contract_conn, "analytics-cross-bucket-1", admin_id)
    await _set_state(contract_conn, paper_id, admin_id, "reading")
    await _set_state(contract_conn, paper_id, other_id, "done")

    async with _client(_pi_app, admin_cookie) as c:
        resp = await c.get("/api/analytics/papers-by-status")

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text}"
    counts = {row["status"]: row["count"] for row in resp.json()}
    total = sum(counts.values())
    assert total == 1, (
        "Admin papers-by-status must count each paper once across all buckets. "
        f"One paper with two different user states yielded sum={total!r}; "
        f"full counts={counts!r}. Expected sum=1."
    )


# ---------------------------------------------------------------------------
# §A-ANALYTICS-02 — non-admin papers-by-status scoped to caller's library
# Verified: services/paper_ingestion/paper_ingestion/services/analytics_queries.py:57
# (non-admin branch JOIN user_library + paper_user_state.user_id=$1)
# ---------------------------------------------------------------------------


async def test_non_admin_papers_by_status_scoped_to_caller(
    contract_conn, _pi_app, _configure_api_key
):
    """A non-admin caller sees only their own library's status counts, not others'.

    Seed a paper in user A's library marked 'reading' and a separate paper in user
    B's library marked 'done'. As user A, only the 'reading' count is visible.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:288
    # (papers_by_status passes is_admin=False for a role='user' caller).
    """
    user_a_id, cookie_a = await _seed_user_with_session(
        contract_conn, "analytics-scope-a@contract.example.com", "user"
    )
    user_b_id, _ = await _seed_user_with_session(
        contract_conn, "analytics-scope-b@contract.example.com", "user"
    )

    paper_a = await _seed_paper(contract_conn, "analytics-scope-a", user_a_id)
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_a_id,
        paper_a,
    )
    await _set_state(contract_conn, paper_a, user_a_id, "reading")

    paper_b = await _seed_paper(contract_conn, "analytics-scope-b", user_b_id)
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_b_id,
        paper_b,
    )
    await _set_state(contract_conn, paper_b, user_b_id, "done")

    async with _client(_pi_app, cookie_a) as c:
        resp = await c.get("/api/analytics/papers-by-status")

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text}"
    counts = {row["status"]: row["count"] for row in resp.json()}
    assert counts.get("reading") == 1, f"User A must see their own 'reading' paper; got {counts!r}"
    assert "done" not in counts, (
        f"User A must not see user B's 'done' paper in scoped counts; got {counts!r}"
    )
