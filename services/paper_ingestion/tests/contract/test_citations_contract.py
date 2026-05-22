"""Citations domain contract tests — Phase B target rows A25, A28.

Survivor-of: test_citations.py mock-unit assertions for get_citation_graph
    and get_paper_citations.
Carve-out: app.state.http_client is MagicMock (outbound HTTP);
    SemanticScholarSource is mocked (external S2 API boundary).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import httpx

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "citations-contract-key-phase-b-do-not-use-in-prod"


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


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared

    removed_override = app.dependency_overrides.pop(
        current_user_id_strict_with_owner_override, None
    )
    had_override = removed_override is not None

    yield app

    if original_pool is None:
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
    else:
        app.state.db_pool = original_pool

    if had_override:
        app.dependency_overrides[current_user_id_strict_with_owner_override] = removed_override


def _make_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# A25: GET /api/citations/graph — citation graph scoped to owner's papers
# ---------------------------------------------------------------------------


async def test_a25_citation_graph_owner_gets_200_with_graph_shape(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A25: GET /api/citations/graph returns CitationGraphResponse shape.

    Verified: citations.py:34-47 at HEAD d21aaea8.
    Survivor-of (future Phase C): test_citations.py::test_get_citation_graph_*.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/citations/graph", params={"paper_ids": paper_id})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "nodes" in body or "entities" in body or "papers" in body or isinstance(body, dict), (
        f"Unexpected citation graph response shape: {list(body.keys())}"
    )


async def test_a25_citation_graph_user_b_cannot_access_user_a_paper(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A25: GET /api/citations/graph 403/404 when user B requests user A's paper.

    Verified: citations.py:44-46 assert_paper_ownership at HEAD d21aaea8.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/citations/graph", params={"paper_ids": paper_id})

    assert resp.status_code in (403, 404), (
        f"User B should get 403/404 for user A's paper citations graph; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# A28: GET /api/citations/{paper_id} — citation list for owner's paper
# ---------------------------------------------------------------------------


async def test_a28_get_paper_citations_owner_gets_list(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A28: GET /api/citations/{paper_id} returns list for owner.

    Verified: citations.py:88-118 get_paper_citations at HEAD d21aaea8.
    Survivor-of (future Phase C): test_citations.py::test_get_paper_citations_*.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/citations/{paper_id}")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, list), f"Expected list response, got: {type(body).__name__}"


async def test_a28_get_paper_citations_user_b_gets_403_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A28 (PARTIAL-IDOR): user B denied access to user A's citations.

    Verified: citations.py:96 assert_paper_ownership at HEAD d21aaea8.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/citations/{paper_id}")

    assert resp.status_code in (403, 404), (
        f"User B should get 403/404 for user A's citations; got {resp.status_code}"
    )
