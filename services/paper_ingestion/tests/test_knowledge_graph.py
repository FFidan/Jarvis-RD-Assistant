"""Tests for knowledge graph feature."""

import sys
import types
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from paper_ingestion.models import (
    EntityCreate,
    EntityDetailResponse,
    EntityExtractionResponse,
    EntityResponse,
    KGQueryResponse,
    KnowledgeGraphResponse,
    RelationshipCreate,
    RelationshipResponse,
)


def _make_pool(conn: AsyncMock):
    """Create a mock pool whose acquire() returns the provided connection."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


class _FakeVectorParams:
    def __init__(self, *, size: int, distance: str):
        self.size = size
        self.distance = distance


def _collection_info(dim: int) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=dim)))
    )


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


def test_entity_create_valid():
    """EntityCreate accepts valid data."""
    e = EntityCreate(name="BERT", entity_type="method", description="A language model")
    assert e.name == "BERT"
    assert e.entity_type == "method"


def test_entity_create_no_description():
    """EntityCreate works without description."""
    e = EntityCreate(name="ImageNet", entity_type="dataset")
    assert e.description is None


def test_entity_response():
    """EntityResponse validates correctly."""
    now = datetime.now(tz=UTC)
    resp = EntityResponse(
        id=1,
        name="BERT",
        canonical_name="bert",
        entity_type="method",
        description="A model",
        paper_count=5,
        created_at=now,
    )
    assert resp.paper_count == 5


def test_relationship_create():
    """RelationshipCreate accepts valid data."""
    r = RelationshipCreate(
        source_entity="BERT",
        target_entity="GLUE",
        relationship_type="evaluates",
        evidence_quote="We evaluate...",
    )
    assert r.relationship_type == "evaluates"


def test_relationship_response():
    """RelationshipResponse validates correctly."""
    now = datetime.now(tz=UTC)
    resp = RelationshipResponse(
        id=1,
        source_entity_id=1,
        target_entity_id=2,
        relationship_type="used_on",
        paper_id=10,
        evidence_quote="Applied to...",
        confidence=0.9,
        created_at=now,
    )
    assert resp.confidence == 0.9


def test_entity_extraction_response():
    """EntityExtractionResponse validates correctly."""
    resp = EntityExtractionResponse(entities_added=5, relationships_added=3, entities_merged=2)
    assert resp.entities_added == 5
    assert resp.entities_merged == 2


def test_knowledge_graph_response():
    """KnowledgeGraphResponse composes entities and relationships."""
    now = datetime.now(tz=UTC)
    graph = KnowledgeGraphResponse(
        entities=[
            EntityResponse(id=1, name="X", canonical_name="x", entity_type="method", created_at=now)
        ],
        relationships=[],
    )
    assert len(graph.entities) == 1


def test_entity_detail_response():
    """EntityDetailResponse validates correctly."""
    now = datetime.now(tz=UTC)
    detail = EntityDetailResponse(
        entity=EntityResponse(
            id=1, name="BERT", canonical_name="bert", entity_type="method", created_at=now
        ),
        relationships=[],
        papers=[{"id": 1, "title": "Paper A", "mention_count": 3}],
    )
    assert len(detail.papers) == 1


def test_kg_query_response():
    """KGQueryResponse validates correctly."""
    resp = KGQueryResponse(results=[{"method": "BERT"}], query="What methods?")
    assert resp.query == "What methods?"


# ---------------------------------------------------------------------------
# Entity normalization tests
# ---------------------------------------------------------------------------


def test_canonical_name_normalization():
    """Canonical names are lowercased and stripped."""
    # This tests the pattern used in _find_or_create_entity
    name = "  BERT  "
    canonical = name.lower().strip()
    assert canonical == "bert"


def test_canonical_name_preserves_hyphens():
    """Canonical names preserve hyphens and special chars."""
    name = "GPT-4"
    canonical = name.lower().strip()
    assert canonical == "gpt-4"


# ---------------------------------------------------------------------------
# Entity extraction tests (with mocks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_or_create_entity_new():
    """Creates a new entity when none exists."""
    from paper_ingestion.extraction.entities import _find_or_create_entity

    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        None,  # No existing entity
        {"id": 42},  # INSERT RETURNING
    ]
    mock_conn.execute = AsyncMock()

    entity_id, was_merged = await _find_or_create_entity(
        mock_conn, "BERT", "method", "A language model", None
    )
    assert entity_id == 42
    assert was_merged is False


@pytest.mark.asyncio
async def test_find_or_create_entity_existing():
    """Returns existing entity when canonical name matches."""
    from paper_ingestion.extraction.entities import _find_or_create_entity

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 99}
    mock_conn.execute = AsyncMock()

    entity_id, was_merged = await _find_or_create_entity(
        mock_conn, "BERT", "method", "A language model", None
    )
    assert entity_id == 99
    assert was_merged is True


@pytest.mark.asyncio
async def test_find_or_create_entity_merges_by_precomputed_similarity():
    """Pre-computed embedding similarity dedup merges into an existing entity id."""
    from paper_ingestion.extraction.entities import _find_or_create_entity

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None  # exact canonical match not found
    mock_conn.execute = AsyncMock()

    entity_id, was_merged = await _find_or_create_entity(
        mock_conn,
        "BERT",
        "method",
        "A language model",
        None,
        similar_entity_id=77,
    )

    assert entity_id == 77
    assert was_merged is True
    mock_conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_or_create_entity_falls_back_to_insert_when_no_similarity():
    """Without a pre-computed similar entity, entity is inserted fresh."""
    from paper_ingestion.extraction.entities import _find_or_create_entity

    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        None,  # exact canonical match not found
        {"id": 55},  # INSERT ... RETURNING id
    ]
    mock_conn.execute = AsyncMock()

    entity_id, was_merged = await _find_or_create_entity(
        mock_conn,
        "BERT",
        "method",
        "A language model",
        None,
        embedding=None,
        similar_entity_id=None,
    )

    assert entity_id == 55
    assert was_merged is False
    mock_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_entity_text_returns_vector():
    """_embed_entity_text returns the first embedding vector."""
    from paper_ingestion.extraction.entities import _embed_entity_text

    embedder = AsyncMock()
    embedder.embed_texts.return_value = [[0.1, 0.2, 0.3]]

    result = await _embed_entity_text(embedder, "method", "BERT")
    assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_entity_text_returns_none_on_failure():
    """_embed_entity_text returns None when the embedder fails."""
    from paper_ingestion.extraction.entities import _embed_entity_text

    embedder = AsyncMock()
    embedder.embed_texts.side_effect = RuntimeError("embedder offline")

    result = await _embed_entity_text(embedder, "method", "BERT")
    assert result is None


@pytest.mark.asyncio
async def test_ensure_kg_collection_creates_missing_collection(monkeypatch):
    """Missing kg_entities collection is created with the configured embedding dimension."""
    from paper_ingestion.extraction.entities import KG_COLLECTION, _ensure_kg_collection

    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    qdrant = AsyncMock()
    qdrant.get_collections.return_value = SimpleNamespace(collections=[])

    fake_models = cast(Any, types.ModuleType("qdrant_client.models"))
    fake_models.Distance = SimpleNamespace(COSINE="cosine")
    fake_models.VectorParams = _FakeVectorParams

    with patch.dict(sys.modules, {"qdrant_client.models": fake_models}):
        await _ensure_kg_collection(qdrant)

    qdrant.create_collection.assert_awaited_once()
    assert qdrant.create_collection.await_args.kwargs["collection_name"] == KG_COLLECTION
    vector_config = qdrant.create_collection.await_args.kwargs["vectors_config"]
    assert vector_config.size == 1024


@pytest.mark.asyncio
async def test_ensure_kg_collection_accepts_matching_existing_dimension(monkeypatch):
    """Existing kg_entities collection is usable when its dimension matches."""
    from paper_ingestion.extraction.entities import KG_COLLECTION, _ensure_kg_collection

    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    qdrant = AsyncMock()
    qdrant.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name=KG_COLLECTION)]
    )
    qdrant.get_collection.return_value = _collection_info(1024)

    fake_models = cast(Any, types.ModuleType("qdrant_client.models"))
    fake_models.Distance = SimpleNamespace(COSINE="cosine")
    fake_models.VectorParams = _FakeVectorParams

    with patch.dict(sys.modules, {"qdrant_client.models": fake_models}):
        await _ensure_kg_collection(qdrant)

    qdrant.get_collection.assert_awaited_once_with(collection_name=KG_COLLECTION)
    qdrant.create_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_kg_collection_rejects_wrong_dimension(monkeypatch):
    """Wrong-dimension kg_entities collection is a clear degraded path."""
    from paper_ingestion.extraction.entities import KG_COLLECTION, _ensure_kg_collection

    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    qdrant = AsyncMock()
    qdrant.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name=KG_COLLECTION)]
    )
    qdrant.get_collection.return_value = _collection_info(768)

    fake_models = cast(Any, types.ModuleType("qdrant_client.models"))
    fake_models.Distance = SimpleNamespace(COSINE="cosine")
    fake_models.VectorParams = _FakeVectorParams

    with patch.dict(sys.modules, {"qdrant_client.models": fake_models}):
        with pytest.raises(RuntimeError, match="Qdrant collection 'kg_entities' has dimension 768"):
            await _ensure_kg_collection(qdrant)

    qdrant.create_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_similar_entity_returns_matched_id():
    """_find_similar_entity returns entity_id from Qdrant match."""
    from paper_ingestion.extraction.entities import _find_similar_entity

    qdrant = AsyncMock()
    qdrant.query_points.return_value = MagicMock(points=[MagicMock(payload={"entity_id": 77})])

    fake_models = cast(Any, types.ModuleType("qdrant_client.models"))
    fake_models.FieldCondition = MagicMock()
    fake_models.Filter = MagicMock()
    fake_models.MatchValue = MagicMock()

    with (
        patch(
            "paper_ingestion.extraction.entities._ensure_kg_collection",
            AsyncMock(return_value=None),
        ),
        patch.dict(sys.modules, {"qdrant_client.models": fake_models}),
    ):
        result = await _find_similar_entity(qdrant, "method", [0.1, 0.2, 0.3])

    assert result == 77


@pytest.mark.asyncio
async def test_find_similar_entity_returns_none_on_failure():
    """_find_similar_entity returns None on Qdrant failure."""
    from paper_ingestion.extraction.entities import _find_similar_entity

    qdrant = AsyncMock()
    qdrant.get_collections.side_effect = RuntimeError("qdrant offline")

    fake_models = cast(Any, types.ModuleType("qdrant_client.models"))
    fake_models.FieldCondition = MagicMock()
    fake_models.Filter = MagicMock()
    fake_models.MatchValue = MagicMock()
    fake_models.Distance = MagicMock()
    fake_models.VectorParams = MagicMock()

    with patch.dict(sys.modules, {"qdrant_client.models": fake_models}):
        result = await _find_similar_entity(qdrant, "method", [0.1, 0.2, 0.3])

    assert result is None


@pytest.mark.asyncio
async def test_find_similar_entity_degrades_on_wrong_collection_dimension(monkeypatch):
    """KG semantic dedup falls back instead of querying a wrong-dimension collection."""
    from paper_ingestion.extraction.entities import KG_COLLECTION, _find_similar_entity

    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    qdrant = AsyncMock()
    qdrant.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name=KG_COLLECTION)]
    )
    qdrant.get_collection.return_value = _collection_info(768)

    fake_models = cast(Any, types.ModuleType("qdrant_client.models"))
    fake_models.FieldCondition = MagicMock()
    fake_models.Filter = MagicMock()
    fake_models.MatchValue = MagicMock()
    fake_models.Distance = SimpleNamespace(COSINE="cosine")
    fake_models.VectorParams = _FakeVectorParams

    with patch.dict(sys.modules, {"qdrant_client.models": fake_models}):
        result = await _find_similar_entity(qdrant, "method", [0.1, 0.2, 0.3])

    assert result is None
    qdrant.query_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_entity_embedding_skips_wrong_collection_dimension(monkeypatch):
    """KG embedding upsert is skipped when collection dimension is wrong."""
    from paper_ingestion.extraction.entities import KG_COLLECTION, _store_entity_embedding

    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    conn = AsyncMock()
    qdrant = AsyncMock()
    qdrant.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name=KG_COLLECTION)]
    )
    qdrant.get_collection.return_value = _collection_info(768)

    fake_models = cast(Any, types.ModuleType("qdrant_client.models"))
    fake_models.Distance = SimpleNamespace(COSINE="cosine")
    fake_models.VectorParams = _FakeVectorParams
    fake_models.PointStruct = MagicMock()

    with patch.dict(sys.modules, {"qdrant_client.models": fake_models}):
        await _store_entity_embedding(conn, qdrant, 1, "BERT", "method", [0.1, 0.2])

    qdrant.upsert.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_entity_prompt():
    """build_entity_prompt generates valid prompt."""
    from paper_ingestion.extraction.entities import build_entity_prompt

    prompt = build_entity_prompt("Test Paper", "Some research text about BERT and GPT.")
    assert "Test Paper" in prompt
    assert "method" in prompt
    assert "dataset" in prompt
    assert "entities" in prompt
    assert "relationships" in prompt


@pytest.mark.asyncio
async def test_get_knowledge_graph_empty():
    """get_knowledge_graph returns empty when no entities match."""
    from paper_ingestion.extraction.entities import get_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    result = await get_knowledge_graph(mock_conn, min_paper_count=1)
    assert result["entities"] == []
    assert result["relationships"] == []


@pytest.mark.asyncio
async def test_get_knowledge_graph_returns_display_sizes_and_type_counts():
    """get_knowledge_graph computes frontend display fields from raw DB rows."""
    from paper_ingestion.extraction.entities import get_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.side_effect = [
        [
            {"id": 1, "name": "BERT", "entity_type": "method", "paper_count": 10},
            {"id": 2, "name": "GLUE", "entity_type": "dataset", "paper_count": 1},
        ],
        [
            {
                "source_entity_id": 1,
                "target_entity_id": 2,
                "relationship_type": "evaluates",
                "confidence": 0.9,
            }
        ],
    ]

    result = await get_knowledge_graph(mock_conn, min_paper_count=1)

    assert result["entity_type_counts"] == {"method": 1, "dataset": 1}
    assert result["entities"][0]["display_size"] == 40
    assert result["entities"][1]["display_size"] == 18
    assert result["relationships"][0]["relationship_type"] == "evaluates"


@pytest.mark.asyncio
async def test_extract_entities_for_paper_returns_counts():
    """extract_entities_for_paper persists valid entities and relationships."""
    from paper_ingestion.extraction.entities import extract_entities_for_paper
    from paper_ingestion.models import VerificationResult

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Paper A"}
    mock_conn.fetch.return_value = [
        {
            "id": 11,
            "chunk_index": 0,
            "content": "BERT evaluates GLUE",
            "page_number": 2,
            "start_char": 0,
            "end_char": 19,
            "embedding_id": None,
            "created_at": datetime.now(tz=UTC),
            "paper_id": 1,
        }
    ]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=1)
    pool = _make_pool(mock_conn)
    http_client = AsyncMock()

    from paper_ingestion.extraction.kg_models import (
        KGEntityCandidate,
        KGExtractionOutput,
        KGRelationshipCandidate,
    )

    kg_result = KGExtractionOutput(
        entities=[
            KGEntityCandidate(name="BERT", type="method", description="encoder"),
            KGEntityCandidate(name="GLUE", type="dataset", description="benchmark"),
        ],
        relationships=[
            KGRelationshipCandidate(
                source="BERT",
                target="GLUE",
                type="evaluates",
                evidence="BERT evaluates GLUE",
                confidence=0.9,
            )
        ],
    )

    verified_result = VerificationResult(
        quote="BERT evaluates GLUE",
        verified=True,
        match_type="exact",
        match_score=1.0,
        matched_text="BERT evaluates GLUE",
        page_number=2,
    )

    with (
        patch(
            "paper_ingestion.extraction.entities.call_llm_structured",
            AsyncMock(return_value=kg_result),
        ),
        patch(
            "paper_ingestion.extraction.entities._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, True)]),
        ),
        patch(
            "paper_ingestion.extraction.entities.QuoteVerifier.verify_quote",
            return_value=verified_result,
        ),
    ):
        result = await extract_entities_for_paper(
            http_client, pool, paper_id=1, openai_client=MagicMock()
        )

    assert result.entities_added == 1
    assert result.entities_merged == 1
    assert result.relationships_added == 1
    assert result.dropped_relationships == 0
    assert mock_conn.execute.await_count == 2
    assert mock_conn.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_extract_entities_for_paper_requires_existing_chunks():
    """extract_entities_for_paper should fail early when a paper has not been processed."""
    from paper_ingestion.extraction.entities import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Paper A"}
    mock_conn.fetch.return_value = []
    pool = _make_pool(mock_conn)

    with pytest.raises(ValueError, match="No chunks found for paper 1"):
        await extract_entities_for_paper(AsyncMock(), pool, paper_id=1)


@pytest.mark.asyncio
async def test_extract_entities_for_paper_skips_unlinked_relationships():
    """Relationships whose source or target entity is not in entity_map are skipped.

    With Instructor/Pydantic, malformed payloads are rejected before reaching
    the function. This test covers the remaining skip path: a relationship
    referencing an entity name that was not in the extracted entities list.
    """
    from paper_ingestion.extraction.entities import extract_entities_for_paper
    from paper_ingestion.extraction.kg_models import (
        KGEntityCandidate,
        KGExtractionOutput,
        KGRelationshipCandidate,
    )

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Paper A"}
    mock_conn.fetch.return_value = [
        {
            "id": 11,
            "chunk_index": 0,
            "content": "BERT evaluates GLUE",
            "page_number": 2,
            "start_char": 0,
            "end_char": 19,
            "embedding_id": None,
            "created_at": datetime.now(tz=UTC),
            "paper_id": 1,
        }
    ]
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = _make_pool(mock_conn)

    # Only "BERT" entity is extracted; relationship references "GLUE" which is
    # not in entity_map — the relationship should be silently skipped.
    kg_result = KGExtractionOutput(
        entities=[
            KGEntityCandidate(name="BERT", type="method", description="encoder"),
        ],
        relationships=[
            KGRelationshipCandidate(
                source="BERT",
                target="GLUE",  # GLUE not in entity_map → skipped
                type="evaluates",
                evidence="BERT evaluates GLUE on the benchmark.",
                confidence=0.9,
            )
        ],
    )

    with (
        patch(
            "paper_ingestion.extraction.entities.call_llm_structured",
            AsyncMock(return_value=kg_result),
        ),
        patch(
            "paper_ingestion.extraction.entities._find_or_create_entity",
            AsyncMock(return_value=(1, False)),
        ),
    ):
        result = await extract_entities_for_paper(
            AsyncMock(), pool, paper_id=1, openai_client=MagicMock()
        )

    assert result.entities_added == 1
    assert result.entities_merged == 0
    assert result.relationships_added == 0
    assert mock_conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_extract_entities_for_paper_requires_existing_paper():
    """extract_entities_for_paper raises a ValueError when the paper row is missing."""
    from paper_ingestion.extraction.entities import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    pool = _make_pool(mock_conn)

    with pytest.raises(ValueError, match="Paper 999 not found"):
        await extract_entities_for_paper(AsyncMock(), pool, paper_id=999)


@pytest.mark.asyncio
async def test_query_knowledge_graph_used_on_pattern():
    """query_knowledge_graph uses the relationship query path for used-on questions."""
    from paper_ingestion.extraction.entities import query_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"method_name": "BERT", "target_name": "GLUE", "relationship_type": "evaluates"}
    ]

    rows = await query_knowledge_graph(mock_conn, "What methods are used on GLUE?")

    assert rows == [
        {"method_name": "BERT", "target_name": "GLUE", "relationship_type": "evaluates"}
    ]
    sql = mock_conn.fetch.await_args.args[0]
    assert "relationship_type IN ('used_on', 'evaluates', 'applied_to')" in sql


@pytest.mark.asyncio
async def test_query_knowledge_graph_generic_pattern_trims_trailing_punctuation():
    """Generic queries should strip punctuation before building the SQL LIKE filter."""
    from paper_ingestion.extraction.entities import query_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"name": "BERT", "paper_id": 1}]

    rows = await query_knowledge_graph(mock_conn, "BERT??")

    assert rows == [{"name": "BERT", "paper_id": 1}]
    assert mock_conn.fetch.await_args.args[1] == "%bert%"


@pytest.mark.asyncio
async def test_query_knowledge_graph_returns_empty_on_missing_tables():
    """Undefined-table failures should degrade to an empty result set."""
    from paper_ingestion.extraction.entities import query_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.side_effect = asyncpg.exceptions.UndefinedTableError("missing")

    rows = await query_knowledge_graph(mock_conn, "What outperforms BM25?")

    assert rows == []


# ---------------------------------------------------------------------------
# Ownership guard tests (IDOR fix — Wave 1 closeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_entities_rejects_unowned_paper(monkeypatch):
    """User B cannot trigger entity extraction for a paper they do not own.

    The endpoint must call assert_paper_ownership before delegating to
    extract_entities_for_paper, so an unowned paper raises 403.
    """
    import paper_ingestion.routers.knowledge_graph as kg_router
    from fastapi import HTTPException

    conn = AsyncMock()
    pool = _make_pool(conn)

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=2),  # user B
    )
    deny = HTTPException(status_code=403, detail="paper not owned by current user")
    ownership_mock = AsyncMock(side_effect=deny)
    monkeypatch.setattr(kg_router, "assert_paper_ownership", ownership_mock)

    extract_mock = AsyncMock()
    monkeypatch.setattr(kg_router, "extract_entities_for_paper", extract_mock)

    with pytest.raises(HTTPException) as exc_info:
        await kg_router.extract_entities.__wrapped__(
            MagicMock(),  # request
            paper_id=1,
            db_pool=pool,
            http_client=AsyncMock(),
            embedder=None,
            qdrant=None,
        )

    assert exc_info.value.status_code == 403
    ownership_mock.assert_awaited_once_with(conn, 1, 2)
    extract_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# batch_extract_entities — admin gate tests (3.18)
# ---------------------------------------------------------------------------


def _make_batch_extract_client(*, user_role: str | None):
    """Return (TestClient, conn, app) with the knowledge_graph router mounted.

    Parameters
    ----------
    user_role:
        Value placed on ``request.state.user_role``.  ``None`` simulates an
        API-key-only caller (no session cookie — allowed through by
        ``require_admin``).  ``"user"`` → 403.  ``"admin"`` → 200.
    """
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import (
        get_db_pool,
        get_http_client,
        get_optional_embedder,
        get_optional_qdrant,
        limiter,
    )
    from paper_ingestion.routers import knowledge_graph as kg_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    conn = AsyncMock()
    conn.fetch.return_value = []  # no papers needing extraction → fast path
    pool = _make_pool(conn)

    app.include_router(kg_router.router)
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_optional_embedder] = lambda: None
    app.dependency_overrides[get_optional_qdrant] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None

    # Override require_admin so we can inject the role into request.state
    # before delegating to the real dependency.
    async def _patched_require_admin(request: Request) -> None:
        if user_role is not None:
            request.state.user_role = user_role
        await require_admin(request)

    app.dependency_overrides[require_admin] = _patched_require_admin

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, conn, app


def test_batch_extract_entities_rejects_non_admin():
    """POST /api/extract-entities/batch returns 403 for a non-admin caller.

    3.18 fix: require_admin dependency must be declared on the route so that
    any browser session with role != 'admin' is rejected before any DB or
    LLM work is triggered.
    """
    tc, conn, app = _make_batch_extract_client(user_role="user")
    try:
        resp = tc.post("/api/extract-entities/batch")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 403, (
        f"Expected 403 (admin gate), got {resp.status_code}: {resp.text}"
    )
    # No DB work should be performed after the rejection
    conn.fetch.assert_not_called()


def test_batch_extract_entities_accepts_admin():
    """POST /api/extract-entities/batch returns 200 for an admin caller.

    3.18 fix: admin-role browser sessions (and API-key-only callers with no
    session cookie) must be allowed through.
    """
    tc, conn, app = _make_batch_extract_client(user_role="admin")
    try:
        resp = tc.post("/api/extract-entities/batch")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body == {"extracted": 0, "failed": 0, "total": 0}


# ---------------------------------------------------------------------------
# M-01: get_graph — user_id scoping (Wave 1 closeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_graph_scopes_entities_to_user(monkeypatch):
    """get_graph returns only entities belonging to the requesting user (M-01).

    User B (id=2) has paper_entities rows for entity 2 only.
    The response must not contain entity 1 (owned by user A / id=1).
    """
    import paper_ingestion.routers.knowledge_graph as kg_router

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=2),  # user B
    )

    # get_knowledge_graph is called with user_id=2 and returns only entity 2
    monkeypatch.setattr(
        kg_router,
        "get_knowledge_graph",
        AsyncMock(
            return_value={
                "entities": [
                    {
                        "id": 2,
                        "name": "GLUE",
                        "canonical_name": "glue",
                        "entity_type": "dataset",
                        "description": None,
                        "metadata": {},
                        "paper_count": 1,
                        "created_at": None,
                        "display_size": 18,
                    }
                ],
                "relationships": [],
                "entity_type_counts": {"dataset": 1},
            }
        ),
    )

    pool = _make_pool(AsyncMock())
    result = await kg_router.get_graph.__wrapped__(
        MagicMock(),
        entity_type=None,
        min_paper_count=1,
        db_pool=pool,
    )

    entity_ids = [e.id for e in result.entities]
    assert entity_ids == [2], f"Expected only entity 2, got {entity_ids}"
    assert 1 not in entity_ids


@pytest.mark.asyncio
async def test_get_knowledge_graph_filters_by_user_id():
    """get_knowledge_graph helper passes user_id to the scoped SQL branch (M-01).

    When user_id is provided the helper must execute the EXISTS-subquery variant
    of the SQL rather than the unscoped variant.
    """
    from paper_ingestion.extraction.entities import get_knowledge_graph

    mock_conn = AsyncMock()
    # First fetch → entities; second fetch → relationships (empty)
    mock_conn.fetch.side_effect = [
        [
            {
                "id": 2,
                "name": "GLUE",
                "canonical_name": "glue",
                "entity_type": "dataset",
                "paper_count": 1,
            }
        ],
        [],
    ]

    result = await get_knowledge_graph(mock_conn, user_id=2)

    assert len(result["entities"]) == 1
    assert result["entities"][0]["id"] == 2
    # Verify the scoped SQL branch was used (contains the EXISTS clause)
    first_call_sql = mock_conn.fetch.call_args_list[0].args[0]
    assert "IS NOT DISTINCT FROM" in first_call_sql


# ---------------------------------------------------------------------------
# M-02: list_entities — user_id scoping (Wave 1 closeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entities_scopes_to_user(monkeypatch):
    """list_entities returns only entities belonging to the requesting user (M-02).

    User B (id=2) must not see entities that only have paper_entities rows for
    user A (id=1).
    """
    import paper_ingestion.routers.knowledge_graph as kg_router

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=2),  # user B
    )

    conn = AsyncMock()
    # Only entity 2 is returned (user B's data)
    conn.fetch.return_value = [
        {
            "id": 2,
            "name": "GLUE",
            "canonical_name": "glue",
            "entity_type": "dataset",
            "description": None,
            "metadata": {},
            "paper_count": 1,
            "created_at": None,
        }
    ]
    pool = _make_pool(conn)

    result = await kg_router.list_entities.__wrapped__(
        MagicMock(),
        entity_type=None,
        limit=50,
        offset=0,
        db_pool=pool,
    )

    entity_ids = [e.id for e in result]
    assert entity_ids == [2], f"Expected only entity 2, got {entity_ids}"
    # Verify the scoped SQL branch was used
    sql_called = conn.fetch.call_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql_called


@pytest.mark.asyncio
async def test_list_entities_unscoped_when_no_user(monkeypatch):
    """list_entities is unscoped when user_id is None (server-to-server path, M-02)."""
    import paper_ingestion.routers.knowledge_graph as kg_router

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=None),  # no session
    )

    conn = AsyncMock()
    conn.fetch.return_value = [
        {
            "id": 1,
            "name": "BERT",
            "canonical_name": "bert",
            "entity_type": "method",
            "description": None,
            "metadata": {},
            "paper_count": 3,
            "created_at": None,
        }
    ]
    pool = _make_pool(conn)

    result = await kg_router.list_entities.__wrapped__(
        MagicMock(),
        entity_type=None,
        limit=50,
        offset=0,
        db_pool=pool,
    )

    assert len(result) == 1
    sql_called = conn.fetch.call_args.args[0]
    # Unscoped branch must NOT contain the user_id filter
    assert "IS NOT DISTINCT FROM" not in sql_called


# ---------------------------------------------------------------------------
# M-03: get_entity_detail — user_id scoping (Wave 1 closeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_detail_rejects_entity_not_owned_by_user(monkeypatch):
    """get_entity_detail returns 404 when the entity has no paper_entities row for
    the requesting user — prevents paper enumeration via entity IDs (M-03).
    """
    import paper_ingestion.routers.knowledge_graph as kg_router
    from fastapi import HTTPException

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=2),  # user B
    )

    conn = AsyncMock()
    # Entity exists in DB
    conn.fetchrow.return_value = {
        "id": 1,
        "name": "BERT",
        "canonical_name": "bert",
        "entity_type": "method",
        "description": None,
        "metadata": {},
        "paper_count": 5,
        "created_at": None,
    }
    # But user B has no paper_entities row for entity 1
    conn.fetchval.return_value = None
    pool = _make_pool(conn)

    with pytest.raises(HTTPException) as exc_info:
        await kg_router.get_entity_detail.__wrapped__(
            MagicMock(),
            entity_id=1,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_entity_detail_papers_scoped_to_user(monkeypatch):
    """get_entity_detail scopes the papers list to the requesting user's
    paper_entities rows, not all papers that mention the entity (M-03).
    """
    import paper_ingestion.routers.knowledge_graph as kg_router

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=2),  # user B
    )

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": 3,
        "name": "ResNet",
        "canonical_name": "resnet",
        "entity_type": "method",
        "description": None,
        "metadata": {},
        "paper_count": 2,
        "created_at": None,
    }
    # visibility check → user B has a row for entity 3
    conn.fetchval.return_value = 1
    # relationships (empty for simplicity)
    # papers scoped to user B (only paper 2)
    conn.fetch.side_effect = [
        [],  # rels
        [{"id": 2, "title": "Paper B", "mention_count": 1}],  # papers for user B
    ]
    pool = _make_pool(conn)

    result = await kg_router.get_entity_detail.__wrapped__(
        MagicMock(),
        entity_id=3,
        db_pool=pool,
    )

    paper_ids = [p["id"] for p in result.papers]
    assert paper_ids == [2], f"Expected only paper 2, got {paper_ids}"
    # Verify scoped SQL was used for the papers fetch
    papers_sql = conn.fetch.call_args_list[1].args[0]
    assert "IS NOT DISTINCT FROM" in papers_sql


# ---------------------------------------------------------------------------
# M-04: kg_query — user_id scoping (Wave 1 closeout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kg_query_scopes_generic_search_to_user(monkeypatch):
    """kg_query scopes the generic entity search to the requesting user (M-04).

    User B must not see entities from user A's paper_entities rows.
    """
    import paper_ingestion.routers.knowledge_graph as kg_router

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=2),  # user B
    )
    monkeypatch.setattr(
        kg_router,
        "query_knowledge_graph",
        AsyncMock(return_value=[{"name": "GLUE", "paper_id": 2}]),
    )

    pool = _make_pool(AsyncMock())
    result = await kg_router.kg_query.__wrapped__(
        MagicMock(),
        q="GLUE",
        db_pool=pool,
    )

    assert result.results == [{"name": "GLUE", "paper_id": 2}]
    # Verify query_knowledge_graph was called with user_id=2
    kg_router.query_knowledge_graph.assert_awaited_once()
    call_kwargs = kg_router.query_knowledge_graph.call_args
    assert call_kwargs.kwargs.get("user_id") == 2


@pytest.mark.asyncio
async def test_query_knowledge_graph_generic_filters_by_user_id():
    """query_knowledge_graph helper applies user_id filter to the generic branch (M-04)."""
    from paper_ingestion.extraction.entities import query_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"name": "GLUE", "paper_id": 2}]

    rows = await query_knowledge_graph(mock_conn, "GLUE", user_id=2)

    assert rows == [{"name": "GLUE", "paper_id": 2}]
    sql_called = mock_conn.fetch.call_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql_called
