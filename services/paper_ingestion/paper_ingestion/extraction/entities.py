"""Canonical location shim — implementation at paper_ingestion.entity_extractor."""

from paper_ingestion.entity_extractor import (
    KG_COLLECTION,
    SIMILARITY_THRESHOLD,
    ConnLike,
    _embed_entity_text,
    _ensure_kg_collection,
    _find_or_create_entity,
    _find_similar_entity,
    _store_entity_embedding,
    build_entity_prompt,
    extract_entities_for_paper,
    get_knowledge_graph,
    query_knowledge_graph,
)

__all__ = [
    "KG_COLLECTION",
    "SIMILARITY_THRESHOLD",
    "ConnLike",
    "_embed_entity_text",
    "_ensure_kg_collection",
    "_find_or_create_entity",
    "_find_similar_entity",
    "_store_entity_embedding",
    "build_entity_prompt",
    "extract_entities_for_paper",
    "get_knowledge_graph",
    "query_knowledge_graph",
]
