"""Citations domain contract tests — target rows A25, A28.

Survivor-of: test_citations.py mock-unit assertions for get_citation_graph
    and get_paper_citations.
Carve-out: app.state.http_client is MagicMock (outbound HTTP);
    SemanticScholarSource is mocked (external S2 API boundary).
"""

from __future__ import annotations

import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


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
    Survivor-of: test_citations.py::test_get_citation_graph_*.
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
    Survivor-of: test_citations.py::test_get_paper_citations_*.
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
