"""Qdrant vector operations for knowledge-graph entity normalisation.

Houses the four private helpers that interact with the kg_entities Qdrant
collection.  Kept separate from entities.py so the Qdrant I/O surface is
easy to locate and replace.
"""

import logging
import uuid
from typing import Any

from paper_ingestion.db_types import ConnLike
from paper_ingestion.ingestion.embedder import (
    extract_qdrant_collection_dimension,
    raise_for_collection_dimension_mismatch,
)

logger = logging.getLogger(__name__)

KG_COLLECTION = "kg_entities"
SIMILARITY_THRESHOLD = 0.92


async def _ensure_kg_collection(qdrant_client: Any) -> None:
    """Ensure the kg_entities Qdrant collection exists."""
    from qdrant_client.models import Distance, VectorParams

    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    embedding_dim = get_paper_ingestion_settings().embedding_dimension

    collections = await qdrant_client.get_collections()
    existing = {c.name for c in collections.collections}
    if KG_COLLECTION not in existing:
        await qdrant_client.create_collection(
            collection_name=KG_COLLECTION,
            vectors_config=VectorParams(size=embedding_dim, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection: %s", KG_COLLECTION)
        return

    collection_info = await qdrant_client.get_collection(collection_name=KG_COLLECTION)
    current_dimension = extract_qdrant_collection_dimension(collection_info)
    raise_for_collection_dimension_mismatch(
        KG_COLLECTION,
        current_dimension,
        expected_dimension=embedding_dim,
    )


async def _embed_entity_text(
    embedder: Any,
    entity_type: str,
    name: str,
) -> list[float] | None:
    """Compute an embedding vector for an entity outside a DB connection scope.

    Returns ``None`` on failure so the caller can fall back to exact-match dedup.
    """
    try:
        embeddings = await embedder.embed_texts([f"{entity_type}: {name}"])
        return embeddings[0] if embeddings else None
    except Exception:
        logger.debug("Embedding call failed for entity %s", name, exc_info=True)
        return None


async def _find_similar_entity(
    qdrant_client: Any,
    entity_type: str,
    embedding: list[float],
) -> int | None:
    """Read Qdrant for a semantically similar entity.

    Collection creation is deferred to the guarded persistence path, so this
    precomputation cannot mutate Qdrant for a stale source generation.
    """
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        results = await qdrant_client.query_points(
            collection_name=KG_COLLECTION,
            query=embedding,
            limit=1,
            score_threshold=SIMILARITY_THRESHOLD,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="entity_type",
                        match=MatchValue(value=entity_type),
                    )
                ]
            ),
            with_payload=True,
        )
        if results.points:
            return results.points[0].payload.get("entity_id")
    except Exception:
        logger.warning("Qdrant similarity search failed for type=%s", entity_type, exc_info=True)
    return None


async def _store_entity_embedding(
    conn: ConnLike,
    qdrant_client: Any,
    entity_id: int,
    name: str,
    entity_type: str,
    embedding: list[float],
) -> None:
    """Persist an entity embedding in Qdrant and record the point id in Postgres."""
    try:
        await _ensure_kg_collection(qdrant_client)
        from qdrant_client.models import PointStruct

        point_id = str(uuid.uuid4())
        await qdrant_client.upsert(
            collection_name=KG_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "entity_id": entity_id,
                        "name": name,
                        "entity_type": entity_type,
                    },
                )
            ],
        )
        await conn.execute(
            "UPDATE entities SET embedding_id = $1 WHERE id = $2",
            point_id,
            entity_id,
        )
    except Exception:
        # Recoverable: storing the embedding is optional; future dedup calls
        # will just fall back to exact canonical-name matching for this entity.
        logger.debug("Failed to store entity embedding for %s", name, exc_info=True)
