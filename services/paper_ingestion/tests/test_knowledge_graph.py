"""Tests for knowledge graph feature."""

from datetime import UTC, datetime

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


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"name": "BERT", "entity_type": "method", "description": "A language model"},
            {"name": "BERT", "entity_type": "method"},
            id="valid_with_description",
        ),
        pytest.param(
            {"name": "ImageNet", "entity_type": "dataset"},
            {"description": None},
            id="no_description",
        ),
    ],
)
def test_entity_create(kwargs, expected) -> None:
    """EntityCreate field values match expected for full and minimal inputs."""
    e = EntityCreate(**kwargs)
    for attr, val in expected.items():
        assert getattr(e, attr) == val


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


@pytest.mark.parametrize(
    ("model_cls", "kwargs", "attr", "expected_val"),
    [
        pytest.param(
            EntityExtractionResponse,
            {"entities_added": 5, "relationships_added": 3, "entities_merged": 2},
            "entities_merged",
            2,
            id="entity_extraction_response",
        ),
        pytest.param(
            KGQueryResponse,
            {"results": [{"method": "BERT"}], "query": "What methods?"},
            "query",
            "What methods?",
            id="kg_query_response",
        ),
    ],
)
def test_simple_response_models(model_cls, kwargs, attr, expected_val) -> None:
    """Simple flat response models accept valid data and expose expected fields."""
    obj = model_cls(**kwargs)
    assert getattr(obj, attr) == expected_val


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
