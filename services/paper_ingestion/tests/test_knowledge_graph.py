"""Tests for knowledge graph feature."""

import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from app.models import (
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
        id=1, name="BERT", canonical_name="bert", entity_type="method",
        description="A model", paper_count=5, created_at=now,
    )
    assert resp.paper_count == 5


def test_relationship_create():
    """RelationshipCreate accepts valid data."""
    r = RelationshipCreate(
        source_entity="BERT", target_entity="GLUE",
        relationship_type="evaluates", evidence_quote="We evaluate...",
    )
    assert r.relationship_type == "evaluates"


def test_relationship_response():
    """RelationshipResponse validates correctly."""
    now = datetime.now(tz=UTC)
    resp = RelationshipResponse(
        id=1, source_entity_id=1, target_entity_id=2,
        relationship_type="used_on", paper_id=10,
        evidence_quote="Applied to...", confidence=0.9,
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
        entities=[EntityResponse(id=1, name="X", canonical_name="x", entity_type="method", created_at=now)],
        relationships=[],
    )
    assert len(graph.entities) == 1


def test_entity_detail_response():
    """EntityDetailResponse validates correctly."""
    now = datetime.now(tz=UTC)
    detail = EntityDetailResponse(
        entity=EntityResponse(id=1, name="BERT", canonical_name="bert", entity_type="method", created_at=now),
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
    from app.entity_extractor import _find_or_create_entity

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
    from app.entity_extractor import _find_or_create_entity

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
    from app.entity_extractor import _find_or_create_entity

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None  # exact canonical match not found
    mock_conn.execute = AsyncMock()

    entity_id, was_merged = await _find_or_create_entity(
        mock_conn, "BERT", "method", "A language model", None,
        similar_entity_id=77,
    )

    assert entity_id == 77
    assert was_merged is True
    mock_conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_or_create_entity_falls_back_to_insert_when_no_similarity():
    """Without a pre-computed similar entity, entity is inserted fresh."""
    from app.entity_extractor import _find_or_create_entity

    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        None,  # exact canonical match not found
        {"id": 55},  # INSERT ... RETURNING id
    ]
    mock_conn.execute = AsyncMock()

    entity_id, was_merged = await _find_or_create_entity(
        mock_conn, "BERT", "method", "A language model", None,
        embedding=None, similar_entity_id=None,
    )

    assert entity_id == 55
    assert was_merged is False
    mock_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_entity_text_returns_vector():
    """_embed_entity_text returns the first embedding vector."""
    from app.entity_extractor import _embed_entity_text

    embedder = AsyncMock()
    embedder.embed_texts.return_value = [[0.1, 0.2, 0.3]]

    result = await _embed_entity_text(embedder, "method", "BERT")
    assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_entity_text_returns_none_on_failure():
    """_embed_entity_text returns None when the embedder fails."""
    from app.entity_extractor import _embed_entity_text

    embedder = AsyncMock()
    embedder.embed_texts.side_effect = RuntimeError("embedder offline")

    result = await _embed_entity_text(embedder, "method", "BERT")
    assert result is None


@pytest.mark.asyncio
async def test_find_similar_entity_returns_matched_id():
    """_find_similar_entity returns entity_id from Qdrant match."""
    from app.entity_extractor import KG_COLLECTION, _find_similar_entity

    qdrant = AsyncMock()
    qdrant.query_points.return_value = MagicMock(
        points=[MagicMock(payload={"entity_id": 77})]
    )

    fake_models = types.ModuleType("qdrant_client.models")
    fake_models.FieldCondition = MagicMock()
    fake_models.Filter = MagicMock()
    fake_models.MatchValue = MagicMock()

    with (
        patch("app.entity_extractor._ensure_kg_collection", AsyncMock(return_value=None)),
        patch.dict(sys.modules, {"qdrant_client.models": fake_models}),
    ):
        result = await _find_similar_entity(qdrant, "method", [0.1, 0.2, 0.3])

    assert result == 77


@pytest.mark.asyncio
async def test_find_similar_entity_returns_none_on_failure():
    """_find_similar_entity returns None on Qdrant failure."""
    from app.entity_extractor import _find_similar_entity

    qdrant = AsyncMock()
    qdrant.get_collections.side_effect = RuntimeError("qdrant offline")

    fake_models = types.ModuleType("qdrant_client.models")
    fake_models.FieldCondition = MagicMock()
    fake_models.Filter = MagicMock()
    fake_models.MatchValue = MagicMock()
    fake_models.Distance = MagicMock()
    fake_models.VectorParams = MagicMock()

    with patch.dict(sys.modules, {"qdrant_client.models": fake_models}):
        result = await _find_similar_entity(qdrant, "method", [0.1, 0.2, 0.3])

    assert result is None


@pytest.mark.asyncio
async def test_build_entity_prompt():
    """build_entity_prompt generates valid prompt."""
    from app.entity_extractor import build_entity_prompt

    prompt = build_entity_prompt("Test Paper", "Some research text about BERT and GPT.")
    assert "Test Paper" in prompt
    assert "method" in prompt
    assert "dataset" in prompt
    assert "entities" in prompt
    assert "relationships" in prompt


@pytest.mark.asyncio
async def test_get_knowledge_graph_empty():
    """get_knowledge_graph returns empty when no entities match."""
    from app.entity_extractor import get_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    result = await get_knowledge_graph(mock_conn, min_paper_count=1)
    assert result["entities"] == []
    assert result["relationships"] == []


@pytest.mark.asyncio
async def test_get_knowledge_graph_returns_display_sizes_and_type_counts():
    """get_knowledge_graph computes frontend display fields from raw DB rows."""
    from app.entity_extractor import get_knowledge_graph

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
    from app.entity_extractor import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Paper A"}
    mock_conn.fetch.return_value = [
        {"id": 11, "chunk_index": 0, "content": "BERT evaluates GLUE", "page_number": 2}
    ]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=1)
    pool = _make_pool(mock_conn)
    http_client = AsyncMock()

    llm_result = {
        "entities": [
            {"name": "BERT", "type": "method", "description": "encoder"},
            {"name": "GLUE", "type": "dataset", "description": "benchmark"},
        ],
        "relationships": [
            {"source": "BERT", "target": "GLUE", "type": "evaluates", "confidence": 0.9}
        ],
    }

    with (
        patch("app.entity_extractor.call_llm", AsyncMock(return_value=llm_result)),
        patch("app.entity_extractor._find_or_create_entity", AsyncMock(side_effect=[(1, False), (2, True)])),
    ):
        result = await extract_entities_for_paper(http_client, pool, paper_id=1)

    assert result.entities_added == 1
    assert result.entities_merged == 1
    assert result.relationships_added == 1
    assert mock_conn.execute.await_count == 2
    assert mock_conn.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_extract_entities_for_paper_requires_existing_chunks():
    """extract_entities_for_paper should fail early when a paper has not been processed."""
    from app.entity_extractor import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Paper A"}
    mock_conn.fetch.return_value = []
    pool = _make_pool(mock_conn)

    with pytest.raises(ValueError, match="No chunks found for paper 1"):
        await extract_entities_for_paper(AsyncMock(), pool, paper_id=1)


@pytest.mark.asyncio
async def test_extract_entities_for_paper_skips_invalid_llm_payload_entries():
    """Malformed entities and unlinked relationships should be ignored, not persisted."""
    from app.entity_extractor import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Paper A"}
    mock_conn.fetch.return_value = [
        {"id": 11, "chunk_index": 0, "content": "BERT evaluates GLUE", "page_number": 2}
    ]
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = _make_pool(mock_conn)

    llm_result = {
        "entities": [
            {"name": "BERT", "type": "method", "description": "encoder"},
            {"name": "", "type": "dataset"},
            "not-a-dict",
        ],
        "relationships": [
            {"source": "BERT", "target": "GLUE", "type": "evaluates"},
            {"source": "BERT", "target": "", "type": "evaluates"},
            "not-a-dict",
        ],
    }

    with (
        patch("app.entity_extractor.call_llm", AsyncMock(return_value=llm_result)),
        patch("app.entity_extractor._find_or_create_entity", AsyncMock(return_value=(1, False))),
    ):
        result = await extract_entities_for_paper(AsyncMock(), pool, paper_id=1)

    assert result.entities_added == 1
    assert result.entities_merged == 0
    assert result.relationships_added == 0
    assert mock_conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_extract_entities_for_paper_requires_existing_paper():
    """extract_entities_for_paper raises a ValueError when the paper row is missing."""
    from app.entity_extractor import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    pool = _make_pool(mock_conn)

    with pytest.raises(ValueError, match="Paper 999 not found"):
        await extract_entities_for_paper(AsyncMock(), pool, paper_id=999)


@pytest.mark.asyncio
async def test_query_knowledge_graph_used_on_pattern():
    """query_knowledge_graph uses the relationship query path for used-on questions."""
    from app.entity_extractor import query_knowledge_graph

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
    from app.entity_extractor import query_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"name": "BERT", "paper_id": 1}]

    rows = await query_knowledge_graph(mock_conn, "BERT??")

    assert rows == [{"name": "BERT", "paper_id": 1}]
    assert mock_conn.fetch.await_args.args[1] == "%bert%"


@pytest.mark.asyncio
async def test_query_knowledge_graph_returns_empty_on_missing_tables():
    """Undefined-table failures should degrade to an empty result set."""
    from app.entity_extractor import query_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.side_effect = asyncpg.exceptions.UndefinedTableError("missing")

    rows = await query_knowledge_graph(mock_conn, "What outperforms BM25?")

    assert rows == []
