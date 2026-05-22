"""Discovery endpoint contract tests (Phase B, B.PI-pulse-rag).

Covers the DB-layer behaviors in POST /api/discover and GET /api/similar/{paper_id}:
  - Ownership enforcement (assert_paper_ownership) for seed papers
  - 404 for missing seed paper IDs
  - Cross-user IDOR guard (user B cannot discover from user A's private paper)

Idiomatic-mock carve-out (KEEP):
  - embedder.discover_from_seeds — Qdrant RecommendQuery boundary (kept mocked)
  - embedder.search_similar — Qdrant query boundary (kept mocked)
  - app.state.http_client = MagicMock() (outbound HTTP boundary)

Contract test targets:
  - Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:125-207
    (POST /api/discover — discover_papers)
  - Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:31-117
    (GET /api/similar/{paper_id} — find_similar_papers)
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "discovery-contract-key-do-not-use-in-prod"


# ---------------------------------------------------------------------------
# App + client fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app(contract_conn):
    """paper_ingestion app wired to the contract connection, rate limiter off."""
    from unittest.mock import AsyncMock, MagicMock

    from paper_ingestion.deps import get_db_pool, get_embedder, limiter
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_embedder = getattr(app.state, "embedder", None)

    # Idiomatic mock for embedder (Qdrant/Ollama boundary)
    mock_embedder = MagicMock()
    mock_embedder.qdrant = MagicMock()  # non-None so 503 is not raised
    mock_embedder.discover_from_seeds = AsyncMock(return_value=[])
    mock_embedder.search_similar = AsyncMock(return_value=[])
    mock_embedder.hybrid_search = AsyncMock(return_value=[])

    app.state.db_pool = shared
    app.state.http_client = MagicMock()
    app.state.embedder = mock_embedder
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[get_embedder] = lambda: mock_embedder

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
        app.dependency_overrides.pop(get_embedder, None)


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


def _client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# §A-DISC-01 — POST /api/discover: 404 for non-existent seed paper IDs
# Verified: discovery.py:152-164 (missing paper_ids → 404)
# ---------------------------------------------------------------------------


async def test_discover_404_for_nonexistent_seed_paper(
    contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/discover returns 404 when seed paper_ids do not exist in DB.

    The ownership check (assert_paper_ownership) or the existence check
    must raise 404 before reaching the Qdrant discovery call.
    """
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": [999999999], "limit": 5},
        )

    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 for non-existent seed paper; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-02 — POST /api/discover: IDOR — user B cannot discover from user A's paper
# Verified: discovery.py:151-164 (assert_paper_ownership for each seed paper_id)
# ---------------------------------------------------------------------------


async def test_discover_idor_user_b_cannot_use_user_a_seed(
    contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/discover: user B cannot seed from a paper owned only by user A.

    assert_paper_ownership raises 403 when the caller is not the paper's owner
    (discovered_by != caller user_id).
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": [paper_id_a], "limit": 5},
        )

    assert resp.status_code in (403, 404), (
        f"IDOR: user B must get 403/404 when seeding from user A's paper {paper_id_a}; "
        f"got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-03 — POST /api/discover: owner gets valid response (empty Qdrant mock)
# Verified: discovery.py:125-207 (discover_papers full path)
# ---------------------------------------------------------------------------


async def test_discover_owner_gets_200_with_empty_results(
    contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/discover returns 200 with empty list when Qdrant returns no results.

    Confirms the ownership check passes for the paper's owner and the DB metadata
    enrichment path is reached without error (even when Qdrant mock returns []).
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": [paper_id_a], "limit": 5},
        )

    assert resp.status_code == 200, (
        f"Owner expected 200 from /api/discover; got {resp.status_code}: {resp.text}"
    )
    assert isinstance(resp.json(), list), "Response must be a list"


# ---------------------------------------------------------------------------
# §A-DISC-04 — POST /api/discover: paper_ids > 200 returns 400
# Verified: discovery.py:148-149 (paper_ids > 200 → 400)
# ---------------------------------------------------------------------------


async def test_discover_400_for_too_many_seed_ids(contract_two_users, _pi_app, _configure_api_key):
    """POST /api/discover returns 400 when paper_ids exceeds 200 items."""
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": list(range(1, 202)), "limit": 5},
        )

    assert resp.status_code == 400, (
        f"Expected 400 for > 200 seed IDs; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-05 — GET /api/similar/{paper_id}: IDOR — user B cannot query user A's paper
# Verified: discovery.py:57-58 (current_user_id_strict + assert_paper_ownership)
# ---------------------------------------------------------------------------


async def test_similar_idor_user_b_cannot_query_user_a_paper(
    contract_two_users, _pi_app, _configure_api_key
):
    """GET /api/similar/{paper_id}: user B cannot find papers similar to user A's paper.

    assert_paper_ownership raises 403 before any Qdrant call.
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/similar/{paper_id_a}")

    assert resp.status_code in (403, 404), (
        f"IDOR: user B must get 403/404 for GET /api/similar/{paper_id_a}; "
        f"got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-06 — GET /api/similar/{paper_id}: 404 for non-existent paper
# Verified: discovery.py:57-63 (assert_paper_ownership → 404 when paper absent)
# ---------------------------------------------------------------------------


async def test_similar_404_for_nonexistent_paper(contract_two_users, _pi_app, _configure_api_key):
    """GET /api/similar/{paper_id} returns 403 or 404 for a non-existent paper_id."""
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/similar/999999999")

    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 for non-existent paper; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-07 — GET /api/similar/{paper_id}: owner gets 200 with empty results
# Verified: discovery.py:31-117 (find_similar_papers full path)
# ---------------------------------------------------------------------------


async def test_similar_owner_gets_200_with_empty_results(
    contract_two_users, _pi_app, _configure_api_key
):
    """GET /api/similar/{paper_id} returns 200 list for the paper's owner.

    Qdrant mock returns [] → enriched list is empty; endpoint still returns 200 [].
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/similar/{paper_id_a}")

    assert resp.status_code == 200, (
        f"Owner expected 200 from /api/similar/{paper_id_a}; got {resp.status_code}: {resp.text}"
    )
    assert isinstance(resp.json(), list), "Response must be a list"
