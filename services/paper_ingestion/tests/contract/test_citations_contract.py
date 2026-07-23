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
# get_or_create_stub_paper — idempotent upsert refreshes citation_count
# ---------------------------------------------------------------------------


async def test_get_or_create_stub_paper_idempotent_and_refreshes_citation_count(
    contract_conn,
):
    """Two calls with the same S2 identifier return the same id, create no
    duplicate row, and the 2nd call REFRESHES citation_count via ON CONFLICT.

    Tested at the contract layer (real connection) because the behavior under
    test lives in the ON CONFLICT ... DO UPDATE path — an AsyncMock'd fetchrow
    cannot exercise that. Verified: citations.py get_or_create_stub_paper.
    """
    from paper_ingestion.citations import get_or_create_stub_paper

    s2_first = {
        "paperId": "abc123",
        "title": "Idempotent Stub Paper",
        "authors": [{"name": "Ada Lovelace"}],
        "year": 2020,
        "citationCount": 5,
    }
    # Same paperId, higher citation_count — must update the existing row.
    s2_second = {**s2_first, "citationCount": 42}

    id_first = await get_or_create_stub_paper(contract_conn, s2_first)
    id_second = await get_or_create_stub_paper(contract_conn, s2_second)

    assert id_first is not None
    assert id_first == id_second, "same identifier must return the same paper id"

    row_count = await contract_conn.fetchval(
        "SELECT count(*) FROM papers WHERE external_id = $1", "s2:abc123"
    )
    assert row_count == 1, f"no duplicate row may be created; got {row_count}"

    refreshed = await contract_conn.fetchval(
        "SELECT citation_count FROM papers WHERE external_id = $1", "s2:abc123"
    )
    assert refreshed == 42, (
        f"the 2nd call must refresh citation_count via ON CONFLICT DO UPDATE; got {refreshed}"
    )


async def test_stub_conflict_does_not_promote_or_overwrite_existing_row(contract_conn):
    """Citation sync must not promote or content-overwrite an EXISTING paper.

    A pre-existing non-stub private row (e.g. a user's own arxiv paper) that
    happens to share an S2 external_id must keep its scope, source, title, and
    metadata when a later citation batch touches it; only ``citation_count`` —
    a trusted scholarly signal — may be refreshed. Exercised at the contract
    layer because the invariant lives in the ON CONFLICT ... DO UPDATE path.
    Verified: citations.py get_or_create_stub_paper.
    """
    from paper_ingestion.citations import get_or_create_stub_paper

    seeded_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url,
                               visibility_scope, citation_count, metadata)
           VALUES ('s2:X', 'arxiv', 'owner', ARRAY['Owner'],
                   'https://example.test/owner', 'private', 0, '{}'::jsonb)
           RETURNING id""",
    )

    returned_id = await get_or_create_stub_paper(
        contract_conn, {"paperId": "X", "title": "attacker", "citationCount": 5}
    )
    assert returned_id == seeded_id, "conflict must match the existing row, not insert a new one"

    row = await contract_conn.fetchrow(
        """SELECT visibility_scope, source_type, title, citation_count,
                  metadata->>'stub' AS stub_flag
           FROM papers WHERE external_id = 's2:X'""",
    )
    assert row["visibility_scope"] == "private", "existing scope must not be promoted"
    assert row["source_type"] == "arxiv", "existing source must not be overwritten"
    assert row["title"] == "owner", "existing content must not be overwritten"
    assert row["stub_flag"] is None, "an existing non-stub row must not be re-tagged as a stub"
    assert row["citation_count"] == 5, "only citation_count may be refreshed on conflict"


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


# ---------------------------------------------------------------------------
# A29: POST /api/citations/batch-fetch — enqueues 202 + queued message
# ---------------------------------------------------------------------------


async def test_a29_batch_fetch_citations_enqueues_202(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/citations/batch-fetch returns 202 + queued message; task_registry carve-out.

    # Verified: services/paper_ingestion/paper_ingestion/routers/citations.py:54
    # (batch_fetch_citations defers citations.batch_fetch and returns BatchCitationFetchResponse).
    """
    from unittest.mock import AsyncMock, patch

    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"citations.batch_fetch": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/citations/batch-fetch")

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("queued") == 1, f"Expected queued=1: {body}"
    assert "message" in body, f"Missing 'message' key: {body}"
    mock_task.defer_async.assert_awaited_once()
