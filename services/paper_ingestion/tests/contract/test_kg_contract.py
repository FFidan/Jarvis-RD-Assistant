"""Knowledge graph domain contract tests — target rows A47, A48, A49.

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
    Survivor-of: test_knowledge_graph.py mock-unit tests for get_graph.
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
    Survivor-of: test_knowledge_graph.py mock-unit tests for list_entities.
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
    Survivor-of: test_kg_relationship_scoping.py mock-unit tests.
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
    Survivor-of: test_knowledge_graph.py relationship-traversal mock tests.
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
    Survivor-of: test_knowledge_graph.py duplicate-entity mock tests.
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
    Survivor-of: test_knowledge_graph.py 404 path mock tests.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph/entity/999999999")

    assert resp.status_code in (403, 404), (
        f"Expected 403/404 for non-existent entity; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# W1A.2 extensions — extract-entities endpoint + kg_query user-scoping
#
# Contract 1: POST /api/extract-entities/{paper_id} triggers Qdrant collection
#             creation with the configured embedding dimension (FauxQdrant).
# Contract 2: POST /api/extract-entities/{paper_id} persists paper_entities
#             scoped to the calling user (two-user IDOR proof).
# Contract 3: GET /api/knowledge-graph/query?q=... returns only caller-scoped
#             entities (SQL user-scoped generic branch).
#
# Verified: knowledge_graph.py:91-130 extract_entities handler
# Verified: extraction/entities.py:88-112 _ensure_kg_collection
# Verified: extraction/entities.py:272-492 extract_entities_for_paper
# Verified: knowledge_graph.py:379-390 kg_query handler
# Verified: extraction/entities.py:637-783 query_knowledge_graph
# ---------------------------------------------------------------------------


async def test_kg_build_collection_created_with_correct_dimension():
    """_ensure_kg_collection creates the kg_entities Qdrant collection with the
    embedding dimension from PaperIngestionSettings (default 2560).

    Exercises the Qdrant boundary adapter directly — no HTTP endpoint needed.
    The session-scoped Postgres contract pool is intentionally NOT required here
    because the collection-creation path is purely an in-memory Qdrant operation.

    # Verified: extraction/entities.py:88-112 _ensure_kg_collection
    # Verified: config.py:95-98 embedding_dimension default = 2560
    """
    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion.config import get_paper_ingestion_settings
    from paper_ingestion.extraction.entities import KG_COLLECTION, _ensure_kg_collection

    faux_qdrant = FauxQdrantClient()
    expected_dim = get_paper_ingestion_settings().embedding_dimension

    # PRE-condition: collection does not exist yet
    assert not await faux_qdrant.collection_exists(KG_COLLECTION), (
        "FauxQdrantClient must start empty"
    )

    await _ensure_kg_collection(faux_qdrant)

    # ASSERT: collection created with correct dimension
    assert await faux_qdrant.collection_exists(KG_COLLECTION), (
        f"_ensure_kg_collection must create '{KG_COLLECTION}'"
    )
    col_info = await faux_qdrant.get_collection(collection_name=KG_COLLECTION)
    actual_dim = col_info.config.params.vectors.size
    assert actual_dim == expected_dim, (
        f"Collection dimension must equal settings.embedding_dimension={expected_dim}; "
        f"got {actual_dim}"
    )

    # IDEMPOTENCY: calling again on existing collection with correct dim must not raise
    await _ensure_kg_collection(faux_qdrant)


async def test_kg_extract_entities_persists_user_scoped(
    contract_two_users,
    contract_conn,
    pi_contract_app_with_litellm_sidecar,
    _configure_api_key,
):
    """POST /api/extract-entities/{paper_id} stamps paper_entities rows with the
    calling user's user_id; user B cannot see those rows.

    # Verified: knowledge_graph.py:102-114 extract_entities passes user_id to helper
    # Verified: extraction/entities.py:398-410 paper_entities INSERT with user_id=$4
    """
    from paper_ingestion._state import set_services
    from jarvis_common.testing_contract_apps import patch_app_state
    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion.extraction.kg_models import (
        KGEntityCandidate,
        KGExtractionOutput,
    )

    app, faux_litellm = pi_contract_app_with_litellm_sidecar

    # Seed paper + chunks owned by user A
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('kg-w1a2-extract', 'arxiv', 'Entity Extraction Test', ARRAY['Author'],
                   'https://kg-w1a2.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_chunks
               (paper_id, chunk_index, content, page_number, start_char, end_char)
           VALUES ($1, 0, 'Transformer method improves GLUE dataset results.', 1, 0, 49)""",
        paper_id,
    )

    # Script a response with one entity
    faux_litellm.add_pydantic_response(
        "fast",
        KGExtractionOutput(
            entities=[KGEntityCandidate(name="Transformer", type="method")],
            relationships=[],
        ),
    )

    import httpx

    faux_qdrant = FauxQdrantClient()
    set_services(openai_client=app.state.openai_client)
    try:
        async with httpx.AsyncClient() as http_client:
            with patch_app_state(app, {"qdrant_client": faux_qdrant, "http_client": http_client}):
                async with _make_client(app, contract_two_users.cookie_a) as c:
                    resp = await c.post(f"/api/extract-entities/{paper_id}")
    finally:
        set_services(openai_client=None)

    assert resp.status_code == 200, (
        f"Expected 200 from extract-entities; got {resp.status_code}: {resp.text[:300]}"
    )

    # ASSERT: paper_entities row stamped with user_a_id
    row_a = await contract_conn.fetchrow(
        "SELECT user_id FROM paper_entities WHERE paper_id = $1 LIMIT 1",
        paper_id,
    )
    assert row_a is not None, "paper_entities row must exist after extraction"
    assert row_a["user_id"] == contract_two_users.user_a_id, (
        f"paper_entities.user_id must equal caller's user_a_id={contract_two_users.user_a_id}; "
        f"got {row_a['user_id']}"
    )

    # ASSERT: user B has no paper_entities rows for this paper (user-scoping invariant)
    row_b = await contract_conn.fetchrow(
        "SELECT 1 FROM paper_entities WHERE paper_id = $1 AND user_id = $2",
        paper_id,
        contract_two_users.user_b_id,
    )
    assert row_b is None, "User B must not have paper_entities rows for user A's extraction"


async def test_kg_query_search_returns_user_scoped_entities(
    contract_two_users,
    contract_conn,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/knowledge-graph/query?q=... returns only entities from caller's KG.

    Seeds one entity for user A and one for user B; queries as user A and verifies
    user B's entity is absent.

    # Verified: knowledge_graph.py:379-390 kg_query passes user_id to query_knowledge_graph
    # Verified: extraction/entities.py:755-769 generic branch user_id IS NOT DISTINCT FROM
    """
    # Seed two entities — one per user
    eid_a = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-query-user-a-entity', 'kg-query-user-a-entity', 'concept', 1)
           RETURNING id"""
    )
    eid_b = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-query-user-b-entity', 'kg-query-user-b-entity', 'concept', 1)
           RETURNING id"""
    )
    b_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('kg-query-b-paper', 'arxiv', 'B Query Paper', ARRAY['Author'],
                   'https://kg-q-b.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_b_id,
    )
    # Link entity A to user A's existing paper, entity B to user B's paper
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        contract_two_users.paper_id_a,
        eid_a,
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        b_paper_id,
        eid_b,
        contract_two_users.user_b_id,
    )

    # Query as user A using the generic branch ("kg-query-user" matches both names)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph/query?q=kg-query-user")

    assert resp.status_code == 200, (
        f"Expected 200 from kg_query; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    names = [r.get("name") for r in body.get("results", [])]

    assert "kg-query-user-a-entity" in names, (
        f"User A's entity must appear in their query results; got names={names}"
    )
    assert "kg-query-user-b-entity" not in names, (
        f"User B's entity must NOT appear in user A's query results; got names={names}"
    )


# ---------------------------------------------------------------------------
# Sidecar-backed KG boundary contracts
#
# Contract 1: _ensure_kg_collection creates kg_entities collection with correct
#             dimension via FauxQdrant; paper_entities table populates correctly.
# Contract 2: _embed_entity_text + _store_entity_embedding persist a vector
#             via FauxOllamaServer(dimension=EMBEDDING_DIMENSION) into FauxQdrant.
# Contract 3: FauxQdrant with wrong-dimension pre-seeded collection raises a
#             clear RuntimeError (not a crash) from _ensure_kg_collection.
# Contract 4: Two-user seeded entities — query_knowledge_graph returns only
#             caller-scoped neighbors (SQL user_id scoping invariant).
# Contract 5: extract_entities_for_paper LLM boundary via faux_litellm sidecar:
#             pydantic response scripted, entities persisted in paper_entities.
#
# Verified: extraction/entities.py:88-112 _ensure_kg_collection
# Verified: extraction/entities.py:115-129 _embed_entity_text
# Verified: extraction/entities.py:167-203 _store_entity_embedding
# Verified: extraction/entities.py:637-783 query_knowledge_graph
# Verified: extraction/entities.py:272-492 extract_entities_for_paper
# ---------------------------------------------------------------------------


# Verified: extraction/entities.py:88-112 _ensure_kg_collection
async def test_kg_w2_collection_setup_creates_with_correct_dimension_via_sidecar(
    contract_conn,
):
    """FauxQdrant receives the correct embedding dimension; paper_entities row populates.

    Combines two assertions:
      (a) _ensure_kg_collection creates kg_entities at EMBEDDING_DIMENSION (2560)
      (b) paper_entities schema accepts a seeded row — exercising the full table contract.
    """
    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion.extraction.entities import KG_COLLECTION, _ensure_kg_collection
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    faux_qdrant = FauxQdrantClient()

    # PRE: collection must not exist
    assert not await faux_qdrant.collection_exists(KG_COLLECTION)

    await _ensure_kg_collection(faux_qdrant)

    # ASSERT (a): correct dimension
    col_info = await faux_qdrant.get_collection(collection_name=KG_COLLECTION)
    assert col_info.config.params.vectors.size == EMBEDDING_DIMENSION, (
        f"kg_entities must be created with dim={EMBEDDING_DIMENSION}; "
        f"got {col_info.config.params.vectors.size}"
    )

    # ASSERT (b): paper_entities schema: seed a row and read it back within the txn
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('w2-dim-test-entity', 'w2-dim-test-entity', 'concept', 1)
           RETURNING id"""
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('kg-w2-dim-paper', 'arxiv', 'W2 Dim Test Paper', ARRAY['A'],
                   'https://w2-dim.test/paper')
           RETURNING id"""
    )
    await contract_conn.execute(
        "INSERT INTO paper_entities (paper_id, entity_id) VALUES ($1, $2)",
        paper_id,
        entity_id,
    )
    row = await contract_conn.fetchrow(
        "SELECT entity_id FROM paper_entities WHERE paper_id = $1",
        paper_id,
    )
    assert row is not None, "paper_entities row must be readable after insert"
    assert row["entity_id"] == entity_id


# Verified: extraction/entities.py:115-129 _embed_entity_text
# Verified: extraction/entities.py:167-203 _store_entity_embedding
async def test_kg_w2_entity_embed_through_faux_ollama_persists_vector(
    contract_conn,
    monkeypatch,
):
    """_embed_entity_text calls embed_texts; _store_entity_embedding persists to Qdrant.

    Full boundary: FauxOllamaServer returns a deterministic 2560-dim vector;
    _store_entity_embedding upserts it to FauxQdrant and records embedding_id in DB.
    """
    import httpx

    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.extraction.entities import KG_COLLECTION, _store_entity_embedding
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder

    # Seed an entity row inside the per-test transaction
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('w2-embed-entity', 'w2-embed-entity', 'method', 1)
           RETURNING id"""
    )

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            faux_qdrant = FauxQdrantClient()
            embedder = Embedder(http_client, faux_qdrant)
            await embedder.ensure_collection()

            # Produce embedding for the entity text
            vectors = await embedder.embed_texts(["method: w2-embed-entity"])
            assert len(vectors) == 1 and len(vectors[0]) == EMBEDDING_DIMENSION, (
                f"embed_texts must return one {EMBEDDING_DIMENSION}-dim vector; "
                f"got {len(vectors)} vectors of dim {len(vectors[0]) if vectors else 'N/A'}"
            )
            embedding = vectors[0]

            # Pre-create kg_entities collection so _store_entity_embedding can upsert
            from qdrant_client.models import Distance, VectorParams  # noqa: PLC0415

            await faux_qdrant.create_collection(
                collection_name=KG_COLLECTION,
                vectors_config=VectorParams(size=EMBEDDING_DIMENSION, distance=Distance.COSINE),
            )

            await _store_entity_embedding(
                contract_conn, faux_qdrant, entity_id, "w2-embed-entity", "method", embedding
            )

    # ASSERT: vector was persisted in FauxQdrant (at least 1 point in kg_entities)
    count_result = await faux_qdrant.count(collection_name=KG_COLLECTION)
    assert count_result.count == 1, (
        f"FauxQdrant kg_entities must have 1 point after _store_entity_embedding; "
        f"got {count_result.count}"
    )

    # ASSERT: embedding_id was written to the entities row in Postgres
    row = await contract_conn.fetchrow("SELECT embedding_id FROM entities WHERE id = $1", entity_id)
    assert row is not None and row["embedding_id"] is not None, (
        f"entities.embedding_id must be populated after _store_entity_embedding; got {row!r}"
    )


# Verified: extraction/entities.py:88-112 _ensure_kg_collection
# Verified: ingestion/embedding_config.py:90-106 raise_for_collection_dimension_mismatch
async def test_kg_w2_dimension_mismatch_returns_graceful_error():
    """_ensure_kg_collection raises RuntimeError (not crash) for wrong-dimension collection.

    FauxQdrant pre-seeded with dim=1024 collection; prod expects EMBEDDING_DIMENSION=2560.
    Verifies the graceful error path (raise_for_collection_dimension_mismatch) fires.
    """
    import pytest as _pytest

    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion.extraction.entities import KG_COLLECTION, _ensure_kg_collection
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    faux_qdrant = FauxQdrantClient()

    # Pre-seed a collection at the WRONG dimension (1024 != 2560)
    wrong_dim = 1024
    assert wrong_dim != EMBEDDING_DIMENSION, "test pre-condition: 1024 must differ from prod dim"

    from qdrant_client.models import Distance, VectorParams  # noqa: PLC0415

    await faux_qdrant.create_collection(
        collection_name=KG_COLLECTION,
        vectors_config=VectorParams(size=wrong_dim, distance=Distance.COSINE),
    )

    # ACT + ASSERT: must raise RuntimeError, not any other exception / crash
    with _pytest.raises(RuntimeError, match="has dimension 1024"):
        await _ensure_kg_collection(faux_qdrant)


# Verified: extraction/entities.py:637-783 query_knowledge_graph generic branch
async def test_kg_w2_similarity_lookup_returns_user_scoped_neighbors(
    contract_two_users,
    contract_conn,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/knowledge-graph/query returns only caller-user's entities.

    Seeds a neighbor entity for user A and a separate entity for user B
    (via paper_entities user_id); queries as user A and verifies user B's
    entity is absent from the neighbors list (SQL user_id IS NOT DISTINCT FROM).
    """
    # Seed user A entity + paper_entities row
    eid_a = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('w2-neighbor-a', 'w2-neighbor-a', 'method', 1)
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        contract_two_users.paper_id_a,
        eid_a,
        contract_two_users.user_a_id,
    )

    # Seed user B entity + own paper
    eid_b = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('w2-neighbor-b', 'w2-neighbor-b', 'method', 1)
           RETURNING id"""
    )
    b_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('kg-w2-neighbor-b', 'arxiv', 'W2 B Neighbor Paper', ARRAY['B'],
                   'https://w2-nb.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_b_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        b_paper_id,
        eid_b,
        contract_two_users.user_b_id,
    )

    # Query as user A — "w2-neighbor" matches both, but only user A's should appear
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph/query?q=w2-neighbor")

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text[:300]}"
    names = [r.get("name") for r in resp.json().get("results", [])]
    assert "w2-neighbor-a" in names, (
        f"User A's entity must appear in their neighbors; got names={names}"
    )
    assert "w2-neighbor-b" not in names, (
        f"User B's entity must NOT appear in user A's neighbors; got names={names}"
    )


# Verified: extraction/entities.py:272-492 extract_entities_for_paper
# Verified: extraction/entities.py:398-410 paper_entities INSERT with user_id=$4
async def test_kg_w2_extract_entities_llm_boundary_via_faux_litellm(
    contract_two_users,
    contract_conn,
    pi_contract_app_with_litellm_sidecar,
    _configure_api_key,
):
    """POST /api/extract-entities/{paper_id} via faux LiteLLM sidecar persists entities.

    The LLM boundary is exercised: faux_litellm.add_pydantic_response scripts a
    KGExtractionOutput with one entity. After the call, asserts:
      (a) paper_entities row exists with user_a_id
      (b) entities table row exists for the scripted entity name
    """
    import httpx

    from jarvis_common.testing_contract_apps import patch_app_state
    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion._state import set_services
    from paper_ingestion.extraction.kg_models import KGEntityCandidate, KGExtractionOutput

    app, faux_litellm = pi_contract_app_with_litellm_sidecar

    # Seed paper + chunk owned by user A
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('kg-w2-llm-bdy', 'arxiv', 'LLM Boundary Test W2', ARRAY['Author'],
                   'https://kg-w2-llm.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_chunks
               (paper_id, chunk_index, content, page_number, start_char, end_char)
           VALUES ($1, 0, 'Transformer architecture was used for classification.', 1, 0, 52)""",
        paper_id,
    )

    # Script LLM response: one entity named "Transformer"
    faux_litellm.add_pydantic_response(
        "fast",
        KGExtractionOutput(
            entities=[KGEntityCandidate(name="Transformer", type="method")],
            relationships=[],
        ),
    )

    faux_qdrant = FauxQdrantClient()
    set_services(openai_client=app.state.openai_client)
    try:
        async with httpx.AsyncClient() as http_client:
            with patch_app_state(app, {"qdrant_client": faux_qdrant, "http_client": http_client}):
                async with _make_client(app, contract_two_users.cookie_a) as c:
                    resp = await c.post(f"/api/extract-entities/{paper_id}")
    finally:
        set_services(openai_client=None)

    assert resp.status_code == 200, (
        f"Expected 200 from extract-entities; got {resp.status_code}: {resp.text[:300]}"
    )

    # ASSERT (a): paper_entities row with user_a_id
    pe_row = await contract_conn.fetchrow(
        "SELECT user_id FROM paper_entities WHERE paper_id = $1 LIMIT 1",
        paper_id,
    )
    assert pe_row is not None, "paper_entities row must exist after extraction"
    assert pe_row["user_id"] == contract_two_users.user_a_id, (
        f"paper_entities.user_id must equal user_a_id={contract_two_users.user_a_id}; "
        f"got {pe_row['user_id']}"
    )

    # ASSERT (b): entities table has the scripted entity
    entity_row = await contract_conn.fetchrow(
        "SELECT id FROM entities WHERE canonical_name = 'transformer' AND entity_type = 'method'"
    )
    assert entity_row is not None, (
        "entities table must have a 'transformer'/'method' row after LLM boundary extraction"
    )


# ---------------------------------------------------------------------------
# TENANT-03: per-user paper_entities rows on a shared paper
#
# Two sub-tests:
#   (a) SQL-layer: directly seed two paper_entities rows for the same
#       (paper_id, entity_id) with different user_ids — proves the
#       UNIQUE NULLS NOT DISTINCT (paper_id, entity_id, user_id) constraint
#       allows per-user rows and that each user's KG is independently scoped.
#   (b) Endpoint-layer: A extracts on A's paper, B extracts on B's paper;
#       the same canonical entity deduplicates to one entities row but each
#       user gets their own paper_entities row (independent mention_counts).
#
# Verified: extraction/entities.py ON CONFLICT (paper_id, entity_id, user_id)
# Migration 0094: UNIQUE NULLS NOT DISTINCT (paper_id, entity_id, user_id)
# ---------------------------------------------------------------------------


async def test_tenant03_constraint_allows_per_user_rows_on_shared_paper(
    contract_two_users,
    contract_conn,
    _pi_app_with_pool,
    _configure_api_key,
):
    """SQL constraint: two users can both have paper_entities rows for the same
    (paper_id, entity_id) pair — each scoped by their own user_id.

    Directly seeds rows via contract_conn to isolate the constraint from the
    endpoint ownership gate. Verifies:
    - INSERT of (paper_id, entity_id, user_a_id) succeeds.
    - INSERT of (paper_id, entity_id, user_b_id) also succeeds (not a conflict).
    - A's KG shows the entity; B's KG shows it too (independent scoping).
    - A re-INSERT triggers ON CONFLICT DO UPDATE (mention_count++) not an error.
    """
    # Seed a shared paper owned by user A
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('tenant03-constraint-paper', 'arxiv', 'Constraint Test Paper TENANT-03',
                   ARRAY['Author'], 'https://tenant03-c.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_a_id,
    )

    # Seed a shared canonical entity
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('GPT-4', 'gpt-4', 'method', 1)
           ON CONFLICT (canonical_name, entity_type)
           DO UPDATE SET paper_count = entities.paper_count + 1
           RETURNING id"""
    )

    # INSERT row for user A — must succeed
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, mention_count, user_id)
           VALUES ($1, $2, 1, $3)
           ON CONFLICT (paper_id, entity_id, user_id) DO UPDATE
           SET mention_count = paper_entities.mention_count + 1""",
        paper_id,
        entity_id,
        contract_two_users.user_a_id,
    )

    # INSERT row for user B on the SAME (paper_id, entity_id) — must NOT conflict with A's row
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, mention_count, user_id)
           VALUES ($1, $2, 1, $3)
           ON CONFLICT (paper_id, entity_id, user_id) DO UPDATE
           SET mention_count = paper_entities.mention_count + 1""",
        paper_id,
        entity_id,
        contract_two_users.user_b_id,
    )

    # ASSERT: both rows exist independently
    row_a = await contract_conn.fetchrow(
        "SELECT mention_count FROM paper_entities WHERE paper_id=$1 AND entity_id=$2 AND user_id=$3",
        paper_id,
        entity_id,
        contract_two_users.user_a_id,
    )
    assert row_a is not None, "User A's paper_entities row must exist"
    assert row_a["mention_count"] == 1

    row_b = await contract_conn.fetchrow(
        "SELECT mention_count FROM paper_entities WHERE paper_id=$1 AND entity_id=$2 AND user_id=$3",
        paper_id,
        entity_id,
        contract_two_users.user_b_id,
    )
    assert row_b is not None, (
        "User B must have her OWN paper_entities row — not blocked by user A's row"
    )
    assert row_b["mention_count"] == 1

    # ASSERT: re-INSERT for A triggers mention_count increment (ON CONFLICT DO UPDATE)
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, mention_count, user_id)
           VALUES ($1, $2, 1, $3)
           ON CONFLICT (paper_id, entity_id, user_id) DO UPDATE
           SET mention_count = paper_entities.mention_count + 1""",
        paper_id,
        entity_id,
        contract_two_users.user_a_id,
    )
    row_a2 = await contract_conn.fetchrow(
        "SELECT mention_count FROM paper_entities WHERE paper_id=$1 AND entity_id=$2 AND user_id=$3",
        paper_id,
        entity_id,
        contract_two_users.user_a_id,
    )
    assert row_a2 is not None
    assert row_a2["mention_count"] == 2, (
        f"Re-insert must increment A's mention_count to 2; got {row_a2['mention_count']}"
    )
    # B's count must remain 1 (A's re-insert did not touch B's row)
    row_b2 = await contract_conn.fetchrow(
        "SELECT mention_count FROM paper_entities WHERE paper_id=$1 AND entity_id=$2 AND user_id=$3",
        paper_id,
        entity_id,
        contract_two_users.user_b_id,
    )
    assert row_b2 is not None
    assert row_b2["mention_count"] == 1, (
        f"A's re-insert must NOT affect B's mention_count; got {row_b2['mention_count']}"
    )

    # ASSERT: A's KG endpoint shows the entity; B's KG shows it too (separate scoping)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        kg_a = await c.get("/api/knowledge-graph")
    assert kg_a.status_code == 200
    names_a = [e["name"].lower() for e in kg_a.json().get("entities", [])]
    assert "gpt-4" in names_a, f"User A's KG must show 'GPT-4'; entities={names_a}"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        kg_b = await c.get("/api/knowledge-graph")
    assert kg_b.status_code == 200
    names_b = [e["name"].lower() for e in kg_b.json().get("entities", [])]
    assert "gpt-4" in names_b, (
        f"User B's KG must show 'GPT-4' via her own paper_entities row; entities={names_b}"
    )


async def test_tenant03_endpoint_per_user_extraction_own_papers(
    contract_two_users,
    contract_conn,
    pi_contract_app_with_litellm_sidecar,
    _configure_api_key,
):
    """Endpoint: A extracts on A's paper, B extracts on B's paper.

    Both papers reference the same canonical entity (same canonical_name/type
    → deduplicated to one entities row). Each user gets their own paper_entities
    row with independent mention_counts. Neither user sees the other's KG entry.

    Verified: extraction/entities.py ON CONFLICT (paper_id, entity_id, user_id)
    """
    import httpx

    from jarvis_common.testing_contract_apps import patch_app_state
    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion._state import set_services
    from paper_ingestion.extraction.kg_models import KGEntityCandidate, KGExtractionOutput

    app, faux_litellm = pi_contract_app_with_litellm_sidecar

    # Seed separate papers — one per user
    paper_a_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('tenant03-ep-paper-a', 'arxiv', 'Paper A TENANT-03',
                   ARRAY['Author'], 'https://tenant03-ep-a.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_chunks
               (paper_id, chunk_index, content, page_number, start_char, end_char)
           VALUES ($1, 0, 'ResNet model outperforms prior baselines on ImageNet.', 1, 0, 52)""",
        paper_a_id,
    )

    paper_b_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('tenant03-ep-paper-b', 'arxiv', 'Paper B TENANT-03',
                   ARRAY['Author'], 'https://tenant03-ep-b.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_b_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_chunks
               (paper_id, chunk_index, content, page_number, start_char, end_char)
           VALUES ($1, 0, 'ResNet achieves top-1 accuracy improvements on ImageNet.', 1, 0, 56)""",
        paper_b_id,
    )

    # Script the same entity name for both extractions — will dedup to one entities row
    for _ in range(2):
        faux_litellm.add_pydantic_response(
            "fast",
            KGExtractionOutput(
                entities=[KGEntityCandidate(name="ResNet", type="method")],
                relationships=[],
            ),
        )

    faux_qdrant = FauxQdrantClient()
    set_services(openai_client=app.state.openai_client)
    try:
        async with httpx.AsyncClient() as http_client:
            with patch_app_state(app, {"qdrant_client": faux_qdrant, "http_client": http_client}):
                async with _make_client(app, contract_two_users.cookie_a) as c:
                    resp_a = await c.post(f"/api/extract-entities/{paper_a_id}")
                assert resp_a.status_code == 200, (
                    f"User A extract-entities failed: {resp_a.status_code} {resp_a.text[:300]}"
                )

                async with _make_client(app, contract_two_users.cookie_b) as c:
                    resp_b = await c.post(f"/api/extract-entities/{paper_b_id}")
                assert resp_b.status_code == 200, (
                    f"User B extract-entities failed: {resp_b.status_code} {resp_b.text[:300]}"
                )
    finally:
        set_services(openai_client=None)

    # ASSERT: A has her paper_entities row (on paper_a_id, user_a_id)
    row_a = await contract_conn.fetchrow(
        "SELECT user_id FROM paper_entities WHERE paper_id = $1 AND user_id = $2",
        paper_a_id,
        contract_two_users.user_a_id,
    )
    assert row_a is not None, "User A must have a paper_entities row on her paper"

    # ASSERT: B has her paper_entities row (on paper_b_id, user_b_id)
    row_b = await contract_conn.fetchrow(
        "SELECT user_id FROM paper_entities WHERE paper_id = $1 AND user_id = $2",
        paper_b_id,
        contract_two_users.user_b_id,
    )
    assert row_b is not None, "User B must have a paper_entities row on her paper"

    # ASSERT: A's KG shows "ResNet"; B's KG shows it independently
    async with _make_client(app, contract_two_users.cookie_a) as c:
        kg_a = await c.get("/api/knowledge-graph")
    assert kg_a.status_code == 200
    names_a = [e["name"].lower() for e in kg_a.json().get("entities", [])]
    assert "resnet" in names_a, (
        f"User A's KG must show 'ResNet' after her extraction; entities={names_a}"
    )

    async with _make_client(app, contract_two_users.cookie_b) as c:
        kg_b = await c.get("/api/knowledge-graph")
    assert kg_b.status_code == 200
    names_b = [e["name"].lower() for e in kg_b.json().get("entities", [])]
    assert "resnet" in names_b, (
        f"User B's KG must show 'ResNet' after her extraction; entities={names_b}"
    )


async def test_a47_graph_paper_count_is_per_user_not_global(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """A shared entity's node paper_count must reflect only the caller's papers.

    Seed one entity whose global entities.paper_count is 3 but which is linked to
    only 1 of user A's papers and 2 of user B's. User A's graph node must report 1,
    never the cross-tenant total of 3.
    """
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-shared-count', 'kg-shared-count', 'concept', 3)
           RETURNING id"""
    )
    await contract_conn.execute(
        "INSERT INTO paper_entities (paper_id, entity_id, user_id) VALUES ($1, $2, $3)"
        " ON CONFLICT DO NOTHING",
        contract_two_users.paper_id_a,
        entity_id,
        contract_two_users.user_a_id,
    )
    for ext in ("kg-b-cnt-1", "kg-b-cnt-2"):
        b_paper = await contract_conn.fetchval(
            """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
               VALUES ($1::text, 'arxiv', 'b count paper', ARRAY['Author'],
                       'https://kg-b.test/' || $1::text, $2)
               RETURNING id""",
            ext,
            contract_two_users.user_b_id,
        )
        await contract_conn.execute(
            "INSERT INTO paper_entities (paper_id, entity_id, user_id) VALUES ($1, $2, $3)"
            " ON CONFLICT DO NOTHING",
            b_paper,
            entity_id,
            contract_two_users.user_b_id,
        )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/knowledge-graph")

    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    node = next((e for e in resp.json()["entities"] if e["name"] == "kg-shared-count"), None)
    assert node is not None, "user A's scoped graph should include the shared entity"
    assert node["paper_count"] == 1, (
        f"node paper_count={node['paper_count']} leaks cross-tenant count (expected 1)"
    )
    assert node["display_size"] == 18, (
        f"display_size={node['display_size']} not derived from per-user count (15 + 1*3 = 18)"
    )


async def test_extract_entities_paper_count_incremented_once_on_reextraction(
    contract_two_users,
    contract_conn,
    pi_contract_app_with_litellm_sidecar,
    _configure_api_key,
):
    """Re-running extraction for the same (paper, entity, user) must NOT inflate
    entities.paper_count: the second POST is a paper_entities ON CONFLICT DO UPDATE
    (no fresh insert), so paper_count stays at 1. On HEAD the per-run set ignores
    DB state and re-increments → paper_count becomes 2.
    """
    from paper_ingestion._state import set_services
    from jarvis_common.testing_contract_apps import patch_app_state
    from jarvis_common.testing_sidecars import FauxQdrantClient
    from paper_ingestion.extraction.kg_models import (
        KGEntityCandidate,
        KGExtractionOutput,
    )
    import httpx

    app, faux_litellm = pi_contract_app_with_litellm_sidecar

    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('kg-dat4-recount', 'arxiv', 'DAT-4 recount', ARRAY['Author'],
                   'https://kg-dat4.test/paper', $1)
           RETURNING id""",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_chunks
               (paper_id, chunk_index, content, page_number, start_char, end_char)
           VALUES ($1, 0, 'Transformer method improves GLUE dataset results.', 1, 0, 49)""",
        paper_id,
    )

    # Two identical scripted responses — one per POST.
    for _ in range(2):
        faux_litellm.add_pydantic_response(
            "fast",
            KGExtractionOutput(
                entities=[KGEntityCandidate(name="Dat4Concept", type="method")],
                relationships=[],
            ),
        )

    faux_qdrant = FauxQdrantClient()
    set_services(openai_client=app.state.openai_client)
    try:
        async with httpx.AsyncClient() as http_client:
            with patch_app_state(app, {"qdrant_client": faux_qdrant, "http_client": http_client}):
                async with _make_client(app, contract_two_users.cookie_a) as c:
                    r1 = await c.post(f"/api/extract-entities/{paper_id}")
                    assert r1.status_code == 200, f"{r1.status_code}: {r1.text[:300]}"
                    r2 = await c.post(f"/api/extract-entities/{paper_id}")
                    assert r2.status_code == 200, f"{r2.status_code}: {r2.text[:300]}"
    finally:
        set_services(openai_client=None)

    # The entity created by the first run; its global paper_count must be 1, not 2.
    paper_count = await contract_conn.fetchval(
        """SELECT e.paper_count FROM entities e
           JOIN paper_entities pe ON pe.entity_id = e.id
           WHERE pe.paper_id = $1 AND pe.user_id = $2
           LIMIT 1""",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert paper_count == 1, (
        f"entities.paper_count={paper_count} — re-extraction double-counted "
        "(must increment only on a genuinely fresh paper_entities insert)"
    )
