"""User-scope tests for citation graph and citation list endpoints.

W1-D1-002: build_citation_graph BFS node fetch leaked non-stub papers from
           other users at BFS depth ≥1.
W1-D2-003: GET /api/citations/{paper_id} counter-party paper IDs leaked
           unscoped.

Test shapes: pure-unit tests against mock asyncpg (make_pool_and_conn) —
these functions are pure DB-query orchestrators with no other I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import FakeRecord, make_pool_and_conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paper_row(
    *,
    paper_id: int,
    title: str = "Test Paper",
    citation_count: int = 0,
    published_date=None,
    metadata: dict | None = None,
) -> FakeRecord:
    return FakeRecord(
        {
            "id": paper_id,
            "title": title,
            "citation_count": citation_count,
            "published_date": published_date,
            "metadata": metadata or {},
        }
    )


def _citation_row(*, source_paper_id: int, cited_paper_id: int) -> FakeRecord:
    return FakeRecord(
        {
            "source_paper_id": source_paper_id,
            "cited_paper_id": cited_paper_id,
            "citation_context": None,
            "is_influential": None,
            "intent": [],
        }
    )


# ---------------------------------------------------------------------------
# W1-D1-002: build_citation_graph must not include papers invisible to caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_citation_graph_filters_other_user_papers() -> None:
    """BFS graph nodes are restricted to papers visible to user_a.

    Scenario:
      - user_a owns P1 (seed).
      - user_b owns P2 (not in user_a's library, discovered_by=user_b_id).
      - P1 cites P2 (one paper_citations row).

    With user_id=user_a_id, _filter_visible_paper_ids must exclude P2
    so it never appears as a node in the returned graph.
    """
    from paper_ingestion.citations import build_citation_graph

    user_a_id = 1
    p1_id = 10
    p2_id = 20  # belongs to user_b — invisible to user_a

    _pool, conn = make_pool_and_conn()

    # Call sequence on conn.fetch:
    # 1. BFS edge expansion: returns the P1→P2 citation row
    # 2. _filter_visible_paper_ids: returns only P1 (P2 excluded)
    # 3. node data fetch: returns P1 row only
    # 4. edge fetch: returns empty (only P1 in all_ids after filter)
    conn.fetch = AsyncMock(
        side_effect=[
            # BFS hop: edges involving {p1_id}
            [FakeRecord({"source_paper_id": p1_id, "cited_paper_id": p2_id})],
            # _filter_visible_paper_ids: p2 is invisible → only p1 returned
            [FakeRecord({"id": p1_id})],
            # node data SELECT
            [_paper_row(paper_id=p1_id, title="Paper A")],
            # edge SELECT (between all_ids which is now [p1_id] only)
            [],
        ]
    )

    result = await build_citation_graph(conn, [p1_id], depth=1, user_id=user_a_id)

    node_ids = {n.id for n in result.nodes}
    assert p1_id in node_ids, "seed paper P1 must be in the graph"
    assert p2_id not in node_ids, (
        "P2 belongs to user_b and must NOT appear as a node for user_a "
        "(W1-D1-002: cross-user paper enumeration via BFS)"
    )


@pytest.mark.asyncio
async def test_build_citation_graph_without_user_id_keeps_all_nodes() -> None:
    """When user_id is None (legacy callers), no filtering is applied."""
    from paper_ingestion.citations import build_citation_graph

    p1_id = 10
    p2_id = 20

    _pool, conn = make_pool_and_conn()

    conn.fetch = AsyncMock(
        side_effect=[
            # BFS hop
            [FakeRecord({"source_paper_id": p1_id, "cited_paper_id": p2_id})],
            # node data (no visibility filter call)
            [
                _paper_row(paper_id=p1_id, title="Paper A"),
                _paper_row(paper_id=p2_id, title="Paper B"),
            ],
            # edges
            [
                FakeRecord(
                    {
                        "source_paper_id": p1_id,
                        "cited_paper_id": p2_id,
                        "is_influential": None,
                        "citation_context": None,
                    }
                )
            ],
        ]
    )

    result = await build_citation_graph(conn, [p1_id], depth=1)  # no user_id

    node_ids = {n.id for n in result.nodes}
    assert p1_id in node_ids
    assert p2_id in node_ids, "without user_id, all BFS nodes should be included"


# ---------------------------------------------------------------------------
# W1-D2-003: GET /api/citations/{paper_id} must strip invisible counter-parties
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_citations_strips_invisible_counter_parties() -> None:
    """Citation rows whose counter-party is not visible to the caller are dropped.

    Scenario:
      - user_a owns P1 (seed, ownership asserted by assert_paper_ownership).
      - P1 has a citation edge to P2 (owned by user_b — invisible to user_a).
      - The endpoint must return an empty list (P2 stripped).
    """
    import httpx
    from httpx import ASGITransport
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    user_a_id = 1
    p1_id = 10
    p2_id = 20  # user_b's paper

    pool, conn = make_pool_and_conn()

    # assert_paper_ownership calls fetchrow to verify caller owns P1
    # Then the router calls fetch for citation rows
    # Then _filter_visible_paper_ids is called for counter-party IDs
    conn.fetchrow = AsyncMock(return_value=FakeRecord({"id": p1_id, "discovered_by": user_a_id}))
    conn.fetch = AsyncMock(
        side_effect=[
            # citation rows: P1→P2
            [_citation_row(source_paper_id=p1_id, cited_paper_id=p2_id)],
            # _filter_visible_paper_ids for [p2_id] → empty (P2 invisible)
            [],
        ]
    )

    app.state.db_pool = pool
    app.state.limiter.enabled = False

    async def override_db_pool():
        return pool

    async def override_api_key():
        return None

    app.dependency_overrides[get_db_pool] = override_db_pool
    app.dependency_overrides[verify_api_key] = override_api_key

    with patch(
        "paper_ingestion.routers.citations.current_user_id_strict",
        new=AsyncMock(return_value=user_a_id),
    ):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/citations/{p1_id}")
        finally:
            app.dependency_overrides.clear()
            app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == [], f"Counter-party P2 (user_b's paper) must be stripped; got: {body}"


@pytest.mark.asyncio
async def test_get_citations_keeps_visible_counter_parties() -> None:
    """Citation rows whose counter-party IS visible to the caller are kept.

    Scenario:
      - user_a owns both P1 and P2.
      - P1 cites P2.
      - _filter_visible_paper_ids returns [p2_id] → row is kept.
    """
    import httpx
    from httpx import ASGITransport
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    user_a_id = 1
    p1_id = 10
    p2_id = 20  # also user_a's paper

    pool, conn = make_pool_and_conn()

    conn.fetchrow = AsyncMock(return_value=FakeRecord({"id": p1_id, "discovered_by": user_a_id}))
    conn.fetch = AsyncMock(
        side_effect=[
            # citation rows: P1→P2
            [_citation_row(source_paper_id=p1_id, cited_paper_id=p2_id)],
            # _filter_visible_paper_ids for [p2_id] → visible
            [FakeRecord({"id": p2_id})],
        ]
    )

    app.state.db_pool = pool
    app.state.limiter.enabled = False

    async def override_db_pool():
        return pool

    async def override_api_key():
        return None

    app.dependency_overrides[get_db_pool] = override_db_pool
    app.dependency_overrides[verify_api_key] = override_api_key

    with patch(
        "paper_ingestion.routers.citations.current_user_id_strict",
        new=AsyncMock(return_value=user_a_id),
    ):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/citations/{p1_id}")
        finally:
            app.dependency_overrides.clear()
            app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1, f"Visible counter-party P2 must be kept; got: {body}"
    assert body[0]["source_paper_id"] == p1_id
    assert body[0]["cited_paper_id"] == p2_id
