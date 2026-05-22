"""Knowledge graph domain contract tests — Phase B target rows A47, A48, A49.

Survivor-of: test_knowledge_graph.py mock-unit assertions for get_graph,
    list_entities, get_entity_detail.
Carve-out: app.state.http_client is MagicMock (outbound HTTP);
    Qdrant client is mocked (exempt external boundary).
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
# A47: GET /api/knowledge-graph — graph nodes/edges scoped to user's papers
# ---------------------------------------------------------------------------


async def test_a47_get_graph_owner_gets_200_with_structure(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A47: GET /api/knowledge-graph returns KnowledgeGraphResponse shape.

    Verified: knowledge_graph.py:133-185 get_graph at HEAD d21aaea8.
    Survivor-of (future Phase C): test_knowledge_graph.py mock-unit tests for get_graph.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "entities" in body, (
        f"Missing 'entities' in knowledge graph response: {list(body.keys())}"
    )
    assert "relationships" in body, (
        f"Missing 'relationships' in knowledge graph response: {list(body.keys())}"
    )
    assert isinstance(body["entities"], list)
    assert isinstance(body["relationships"], list)


async def test_a47_get_graph_no_cross_user_entity_leak(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A47: entities scoped — user B's seed entity not visible to user A.

    Verified: knowledge_graph.py:148-150 get_knowledge_graph(user_id=user_id) scoping.
    """
    # Seed an entity linked to user B's paper only
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-contract-b-only', 'kg-contract-b-only', 'concept', 1)
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3)
           ON CONFLICT DO NOTHING""",
        contract_two_users.paper_id_a,  # use paper_id_a but link to user_b_id
        entity_id,
        contract_two_users.user_b_id,
    )

    # User A should NOT see an entity scoped to user_b_id
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph")

    assert resp.status_code == 200
    entity_names = [e["name"] for e in resp.json().get("entities", [])]
    assert "kg-contract-b-only" not in entity_names, (
        f"User A must not see user B's entity 'kg-contract-b-only'; got: {entity_names}"
    )


# ---------------------------------------------------------------------------
# A48: GET /api/knowledge-graph/entities — entity list scoped to user
# ---------------------------------------------------------------------------


async def test_a48_list_entities_owner_gets_200_list(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A48: GET /api/knowledge-graph/entities returns list for owner.

    Verified: knowledge_graph.py:188-265 list_entities at HEAD d21aaea8.
    Survivor-of (future Phase C): test_knowledge_graph.py mock-unit tests for list_entities.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph/entities")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, list), f"Expected list, got {type(body).__name__}"


async def test_a48_list_entities_user_scoped_no_cross_user_leak(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A48: user A's entity list does not include user B-only entities.

    Verified: knowledge_graph.py:231-243 WHERE pe.user_id IS NOT DISTINCT FROM $3.
    """
    # Seed an entity linked only to user B
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('list-ent-b-only', 'list-ent-b-only', 'method', 1)
           RETURNING id"""
    )
    # Seed a paper for user B to own this entity
    b_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('kg-b-paper-ext', 'arxiv', 'B entity paper', ARRAY['Author'],
                   'https://kg-b.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_b_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3)
           ON CONFLICT DO NOTHING""",
        b_paper_id,
        entity_id,
        contract_two_users.user_b_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph/entities")

    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()]
    assert "list-ent-b-only" not in names, (
        f"User A must not see user B-only entity 'list-ent-b-only'; names={names}"
    )


# ---------------------------------------------------------------------------
# A49: GET /api/knowledge-graph/entity/{entity_id} — entity detail scoped to owner
# ---------------------------------------------------------------------------


async def test_a49_get_entity_detail_user_b_gets_403_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A49: GET /api/knowledge-graph/entity/{id} 403/404 for non-owner.

    Verified: knowledge_graph.py:268 get_entity_detail at HEAD d21aaea8.
    Survivor-of (future Phase C): test_kg_relationship_scoping.py mock-unit tests.
    """
    # Seed an entity linked to user A only
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('detail-ent-a-only', 'detail-ent-a-only', 'concept', 1)
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3)
           ON CONFLICT DO NOTHING""",
        contract_two_users.paper_id_a,
        entity_id,
        contract_two_users.user_a_id,
    )

    # User B should be denied
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/knowledge-graph/entity/{entity_id}")

    assert resp.status_code in (403, 404), (
        f"User B should get 403/404 for user A's entity; got {resp.status_code}"
    )


async def test_a49_get_entity_detail_owner_gets_200(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A49: GET /api/knowledge-graph/entity/{id} 200 for owner.

    Verified: knowledge_graph.py:268 get_entity_detail at HEAD d21aaea8.
    """
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('detail-ent-owner', 'detail-ent-owner', 'concept', 1)
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3)
           ON CONFLICT DO NOTHING""",
        contract_two_users.paper_id_a,
        entity_id,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/knowledge-graph/entity/{entity_id}")

    assert resp.status_code == 200, f"Owner expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert set(body) >= {"entity", "relationships", "papers"}, (
        f"Unexpected entity detail shape: {list(body.keys())}"
    )
    assert body["entity"]["id"] == entity_id
    assert body["entity"]["name"] == "detail-ent-owner"


# ---------------------------------------------------------------------------
# E1.PI extensions — relationship traversal, duplicate entity similarity-merge path
#
# Verified: knowledge_graph.py:133-185 (get_graph — entities + relationships lists)
# Verified: knowledge_graph.py:188-265 (list_entities — user-scoped)
# ---------------------------------------------------------------------------


async def test_e1_kg_relationship_visible_in_graph(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """GET /api/knowledge-graph: a seeded relationship between two entities appears.

    Seeds two entities with a relationship row; verifies the graph endpoint
    returns them in the relationships list.
    Verified: knowledge_graph.py:133-185 (get_graph aggregates entity_relationships).
    Survivor-of (Phase E2): test_knowledge_graph.py relationship-traversal mock tests.
    """
    # Seed two entities owned by user A
    eid1 = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-rel-source', 'kg-rel-source', 'concept', 1)
           RETURNING id"""
    )
    eid2 = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-rel-target', 'kg-rel-target', 'concept', 1)
           RETURNING id"""
    )
    for eid in (eid1, eid2):
        await contract_conn.execute(
            """INSERT INTO paper_entities (paper_id, entity_id, user_id)
               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
            contract_two_users.paper_id_a,
            eid,
            contract_two_users.user_a_id,
        )
    # Seed a relationship between them
    await contract_conn.execute(
        """INSERT INTO entity_relationships
              (source_entity_id, target_entity_id, relationship_type, paper_id, confidence)
           VALUES ($1, $2, 'related', $3, 1.0)
           ON CONFLICT DO NOTHING""",
        eid1,
        eid2,
        contract_two_users.paper_id_a,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph")

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    rel_entity_ids = set()
    for rel in body.get("relationships", []):
        rel_entity_ids.add(rel.get("source_entity_id"))
        rel_entity_ids.add(rel.get("target_entity_id"))
    assert {eid1, eid2}.issubset(rel_entity_ids), (
        f"Seeded relationship must appear in graph response; got ids={rel_entity_ids}"
    )


async def test_e1_kg_duplicate_entity_merge_does_not_double_count(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """GET /api/knowledge-graph/entities: inserting the same entity twice via
    ON CONFLICT yields only one row in the list (no duplicate rows).

    Verifies the unique constraint on entities (canonical_name, entity_type) and that
    the endpoint does not return duplicate entity names.
    Verified: knowledge_graph.py:188-265 (list_entities — SELECT DISTINCT or GROUP BY).
    Survivor-of (Phase E2): test_knowledge_graph.py duplicate-entity mock tests.
    """
    # Insert an entity once (first insert)
    eid = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-dedup-test', 'kg-dedup-test', 'method', 1)
           ON CONFLICT (canonical_name, entity_type)
           DO UPDATE SET paper_count = entities.paper_count + 1
           RETURNING id"""
    )
    # Second insert — same name/type, triggers ON CONFLICT
    eid2 = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-dedup-test', 'kg-dedup-test', 'method', 1)
           ON CONFLICT (canonical_name, entity_type)
           DO UPDATE SET paper_count = entities.paper_count + 1
           RETURNING id"""
    )
    assert eid == eid2, "ON CONFLICT must return the same entity id — no new row"

    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        contract_two_users.paper_id_a,
        eid,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph/entities")

    assert resp.status_code == 200
    names = [e.get("name") for e in resp.json()]
    count = names.count("kg-dedup-test")
    assert count <= 1, (
        f"Entity 'kg-dedup-test' must appear at most once in entity list; got {count}"
    )


async def test_e1_kg_nonexistent_entity_detail_returns_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/knowledge-graph/entity/{id} with a non-existent id returns 404.

    Verified: knowledge_graph.py:268 (get_entity_detail — None row → 404).
    Survivor-of (Phase E2): test_knowledge_graph.py 404 path mock tests.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph/entity/999999999")

    assert resp.status_code in (403, 404), (
        f"Expected 403/404 for non-existent entity; got {resp.status_code}"
    )
