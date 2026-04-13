"""Knowledge graph entity extraction from papers.

Extracts entities (methods, datasets, metrics, concepts) and relationships
from paper text using LLM. Normalizes entities via embedding similarity
in a dedicated Qdrant collection.
"""

import logging
import os
import uuid
from typing import Any

import asyncpg
import httpx
from jarvis_common import get_fast_model
from jarvis_common.llm_client import ChatCompletionOptions, call_llm

from app.converters import row_to_chunk_response  # pyright: ignore[reportUnusedImport]
from app.models import EntityExtractionResponse
from app.verification import QuoteVerifier  # pyright: ignore[reportUnusedImport]

logger = logging.getLogger(__name__)

KG_COLLECTION = "kg_entities"
SIMILARITY_THRESHOLD = 0.92

ConnLike = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]


def build_entity_prompt(title: str, text: str) -> str:
    """Build the knowledge-graph extraction prompt for a single paper."""
    return f"""You are a knowledge graph extractor for research papers. \
Extract entities and relationships from the following paper.

PAPER TITLE: {title}

ENTITY TYPES to extract:
- method: algorithms, techniques, approaches, models
- dataset: datasets, benchmarks, corpora
- metric: evaluation metrics, measures
- concept: key concepts, theories, paradigms
- institution: universities, companies, labs
- author: notable researchers mentioned (not the paper's own authors)

RELATIONSHIP TYPES:
- used_on: method/metric applied to dataset
- outperforms: method outperforms another method
- extends: method extends/builds upon another
- evaluates: metric used to evaluate method
- proposes: paper proposes a new method/concept
- affiliated_with: author affiliated with institution

RULES:
1. Extract 3-15 entities that are central to the paper's contribution
2. Extract 2-10 relationships between the entities
3. For each entity: provide name, type, and brief description
4. For each relationship: provide source, target, type, and supporting evidence quote
5. Use exact entity names as they appear in the paper
6. Only include relationships that are explicitly supported by the text

PAPER TEXT:
{text[:12000]}

Respond with ONLY a JSON object with two keys: "entities" and "relationships".
Example format:
{{
  "entities": [
    {{"name": "BERT", "type": "method", "description": "Bidirectional encoder"}}
  ],
  "relationships": [
    {{"source": "BERT", "target": "GLUE", "type": "evaluates",
      "evidence": "We evaluate BERT on the GLUE benchmark"}}
  ]
}}

JSON:"""


async def _ensure_kg_collection(qdrant_client: Any) -> None:
    """Ensure the kg_entities Qdrant collection exists."""
    from qdrant_client.models import Distance, VectorParams

    embedding_dim = int(os.environ.get("EMBEDDING_DIMENSION", "768"))

    collections = await qdrant_client.get_collections()
    existing = {c.name for c in collections.collections}
    if KG_COLLECTION not in existing:
        await qdrant_client.create_collection(
            collection_name=KG_COLLECTION,
            vectors_config=VectorParams(size=embedding_dim, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection: %s", KG_COLLECTION)


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
    """Search Qdrant for a semantically similar entity (no DB connection needed).

    Returns the matched ``entity_id`` or ``None``.
    """
    try:
        await _ensure_kg_collection(qdrant_client)
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
        logger.debug("Qdrant similarity search failed for type=%s", entity_type, exc_info=True)
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


async def _find_or_create_entity(
    conn: ConnLike,
    name: str,
    entity_type: str,
    description: str | None,
    qdrant_client: Any | None,
    *,
    embedding: list[float] | None = None,
    similar_entity_id: int | None = None,
) -> tuple[int, bool]:
    """Resolve an entity id via exact-match lookup, then optional vector dedup.

    Embedding computation and Qdrant similarity search must be performed
    **before** calling this function (outside any DB connection scope) so
    that long-running HTTP calls do not hold a database connection.  Pass
    the pre-computed results via *embedding* and *similar_entity_id*.
    """
    canonical = name.lower().strip()

    existing = await conn.fetchrow(
        "SELECT id FROM entities WHERE canonical_name = $1 AND entity_type = $2",
        canonical,
        entity_type,
    )
    if existing:
        await conn.execute(
            "UPDATE entities SET paper_count = paper_count + 1 WHERE id = $1",
            existing["id"],
        )
        return existing["id"], True

    # Use pre-computed similarity result from Qdrant (computed outside conn scope)
    if similar_entity_id is not None:
        await conn.execute(
            "UPDATE entities SET paper_count = paper_count + 1 WHERE id = $1",
            similar_entity_id,
        )
        return similar_entity_id, True

    row = await conn.fetchrow(
        """INSERT INTO entities (name, canonical_name, entity_type, description)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (canonical_name, entity_type) DO UPDATE
           SET paper_count = entities.paper_count + 1
           RETURNING id""",
        name,
        canonical,
        entity_type,
        description,
    )
    entity_id: int = row["id"]  # type: ignore[index]

    if embedding is not None and qdrant_client is not None:
        await _store_entity_embedding(
            conn,
            qdrant_client,
            entity_id,
            name,
            entity_type,
            embedding,
        )

    return entity_id, False


async def extract_entities_for_paper(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    paper_id: int,
    embedder: Any | None = None,
    qdrant_client: Any | None = None,
) -> EntityExtractionResponse:
    """Extract and persist entities and relationships for one paper."""
    async with db_pool.acquire() as conn:
        paper = await conn.fetchrow("SELECT id, title FROM papers WHERE id = $1", paper_id)
        if not paper:
            raise ValueError(f"Paper {paper_id} not found")

        chunks = await conn.fetch(
            """SELECT id, chunk_index, content, page_number,
                      start_char, end_char, embedding_id, created_at, paper_id
               FROM paper_chunks WHERE paper_id = $1
               ORDER BY chunk_index""",
            paper_id,
        )
        if not chunks:
            raise ValueError(f"No chunks found for paper {paper_id}")

        fast_model = get_fast_model()

    chunk_responses = [row_to_chunk_response(c) for c in chunks]
    full_text = "\n\n".join(c.content for c in chunk_responses)
    if len(full_text) > 12000:
        full_text = full_text[:12000]

    prompt = build_entity_prompt(paper["title"], full_text)
    try:
        llm_result = await call_llm(
            http_client,
            prompt,
            options=ChatCompletionOptions(model=fast_model),
        )
    except Exception:
        logger.exception("Entity extraction LLM call failed for paper %d", paper_id)
        raise

    entities_data = llm_result.get("entities", [])
    relationships_data = llm_result.get("relationships", [])

    valid_types = {"method", "dataset", "metric", "author", "institution", "concept"}

    entities_added = 0
    entities_merged = 0
    relationships_added = 0
    dropped_count = 0

    entity_map: dict[str, int] = {}

    # --- Phase 1: validate and pre-embed entities (no DB connection held) ---
    valid_entities: list[dict] = []
    for ent in entities_data:
        if not isinstance(ent, dict):
            continue
        name = (ent.get("name") or "").strip()
        etype = (ent.get("type") or "").strip().lower()
        if not name or etype not in valid_types:
            continue
        valid_entities.append({"name": name, "type": etype, "description": ent.get("description")})

    # Pre-compute embeddings and similarity matches outside DB connection scope
    # so that long-running HTTP calls do not hold a database connection (PI-006).
    precomputed: list[dict] = []
    for ve in valid_entities:
        embedding: list[float] | None = None
        similar_entity_id: int | None = None
        if embedder and qdrant_client:
            embedding = await _embed_entity_text(embedder, ve["type"], ve["name"])
            if embedding is not None:
                similar_entity_id = await _find_similar_entity(
                    qdrant_client,
                    ve["type"],
                    embedding,
                )
        precomputed.append(
            {
                "embedding": embedding,
                "similar_entity_id": similar_entity_id,
            }
        )

    # Instantiate verifier once for this extraction run
    quote_verifier = QuoteVerifier()

    # --- Phase 2: DB reads + writes (connection held, no external HTTP) ---
    async with db_pool.acquire() as conn:
        for ve, pc in zip(valid_entities, precomputed):
            entity_id, was_merged = await _find_or_create_entity(
                conn,
                ve["name"],
                ve["type"],
                ve["description"],
                qdrant_client,
                embedding=pc["embedding"],
                similar_entity_id=pc["similar_entity_id"],
            )
            entity_map[ve["name"].lower()] = entity_id

            if was_merged:
                entities_merged += 1
            else:
                entities_added += 1

            first_chunk_id = chunks[0]["id"] if chunks else None
            await conn.execute(
                """INSERT INTO paper_entities (paper_id, entity_id, mention_count, first_chunk_id)
                   VALUES ($1, $2, 1, $3)
                   ON CONFLICT (paper_id, entity_id) DO UPDATE
                   SET mention_count = paper_entities.mention_count + 1""",
                paper_id,
                entity_id,
                first_chunk_id,
            )

        for rel in relationships_data:
            if not isinstance(rel, dict):
                continue
            source_name = (rel.get("source") or "").strip().lower()
            target_name = (rel.get("target") or "").strip().lower()
            rel_type = (rel.get("type") or "").strip()
            evidence = rel.get("evidence")

            source_id = entity_map.get(source_name)
            target_id = entity_map.get(target_name)

            if not source_id or not target_id or not rel_type:
                continue

            try:
                confidence = float(rel.get("confidence", 1.0))
            except (ValueError, TypeError):
                confidence = 1.0

            # --- Anti-hallucination: verify evidence quote before persisting ---
            if evidence:
                vr = quote_verifier.verify_quote(evidence, full_text, chunk_responses)
                if not vr.verified:
                    logger.info(
                        "dropping unverified kg edge: subject=%s predicate=%s object=%s",
                        source_name,
                        rel_type,
                        target_name,
                    )
                    dropped_count += 1
                    continue
                verified_evidence: str | None = vr.matched_text
            else:
                # No evidence provided — treat as unverified, drop the row
                logger.info(
                    "dropping kg edge with no evidence: subject=%s predicate=%s object=%s",
                    source_name,
                    rel_type,
                    target_name,
                )
                dropped_count += 1
                continue

            inserted = await conn.fetchval(
                """INSERT INTO entity_relationships
                       (source_entity_id, target_entity_id, relationship_type,
                        paper_id, evidence_quote, confidence)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (source_entity_id, target_entity_id, relationship_type, paper_id)
                   DO NOTHING
                   RETURNING 1""",
                source_id,
                target_id,
                rel_type,
                paper_id,
                verified_evidence,
                confidence,
            )
            if inserted is not None:
                relationships_added += 1

    return EntityExtractionResponse(
        entities_added=entities_added,
        relationships_added=relationships_added,
        entities_merged=entities_merged,
        dropped_relationships=dropped_count,
    )


async def get_knowledge_graph(
    conn: ConnLike,
    entity_type: str | None = None,
    min_paper_count: int = 1,
    limit: int = 200,
) -> dict:
    """Get the full knowledge graph or a filtered subset."""
    try:
        if entity_type:
            entities = await conn.fetch(
                """SELECT * FROM entities
                   WHERE entity_type = $1 AND paper_count >= $2
                   ORDER BY paper_count DESC LIMIT $3""",
                entity_type,
                min_paper_count,
                limit,
            )
        else:
            entities = await conn.fetch(
                """SELECT * FROM entities
                   WHERE paper_count >= $1
                   ORDER BY paper_count DESC LIMIT $2""",
                min_paper_count,
                limit,
            )
    except asyncpg.exceptions.UndefinedTableError:
        return {"entities": [], "relationships": []}

    entity_ids = [e["id"] for e in entities]
    if not entity_ids:
        return {"entities": [], "relationships": []}

    relationships = await conn.fetch(
        """SELECT * FROM entity_relationships
           WHERE source_entity_id = ANY($1) AND target_entity_id = ANY($1)
           ORDER BY confidence DESC""",
        entity_ids,
    )

    entity_dicts = []
    entity_type_counts: dict[str, int] = {}
    for e in entities:
        d = dict(e)
        paper_count = d.get("paper_count", 1)
        d["display_size"] = min(40, max(15, 15 + paper_count * 3))
        etype = d.get("entity_type", "unknown")
        entity_type_counts[etype] = entity_type_counts.get(etype, 0) + 1
        entity_dicts.append(d)

    return {
        "entities": entity_dicts,
        "relationships": [dict(r) for r in relationships],
        "entity_type_counts": entity_type_counts,
    }


async def query_knowledge_graph(
    conn: ConnLike,
    query: str,
) -> list[dict]:
    """Answer a knowledge graph query using SQL pattern matching on entities."""
    # Simple keyword extraction for SQL matching
    query_lower = query.lower()

    # Detect query pattern
    try:
        if "used on" in query_lower or "applied to" in query_lower:
            # "What methods are used on dataset X?"
            # Extract the target entity name
            target_name = ""
            for keyword in ["used on", "applied to"]:
                if keyword in query_lower:
                    target_name = query_lower.split(keyword)[-1].strip().rstrip("?. ")
                    break

            rows = await conn.fetch(
                """SELECT e1.name AS method_name, e1.entity_type AS method_type,
                          e2.name AS target_name, e2.entity_type AS target_type,
                          er.relationship_type, er.evidence_quote, er.confidence
                   FROM entity_relationships er
                   JOIN entities e1 ON er.source_entity_id = e1.id
                   JOIN entities e2 ON er.target_entity_id = e2.id
                   WHERE LOWER(e2.name) LIKE $1
                     AND er.relationship_type IN ('used_on', 'evaluates', 'applied_to')
                   ORDER BY er.confidence DESC""",
                f"%{target_name}%",
            )
            return [dict(r) for r in rows]

        elif "outperforms" in query_lower or "better than" in query_lower:
            rows = await conn.fetch(
                """SELECT e1.name AS method_name, e2.name AS compared_to,
                          er.evidence_quote, er.confidence
                   FROM entity_relationships er
                   JOIN entities e1 ON er.source_entity_id = e1.id
                   JOIN entities e2 ON er.target_entity_id = e2.id
                   WHERE er.relationship_type = 'outperforms'
                   ORDER BY er.confidence DESC
                   LIMIT 50""",
            )
            return [dict(r) for r in rows]

        else:
            # Generic: search entities by name
            rows = await conn.fetch(
                """SELECT e.*, pe.paper_id,
                          (SELECT title FROM papers p WHERE p.id = pe.paper_id) AS paper_title
                   FROM entities e
                   JOIN paper_entities pe ON e.id = pe.entity_id
                   WHERE LOWER(e.name) LIKE $1
                   ORDER BY e.paper_count DESC
                   LIMIT 20""",
                f"%{query_lower.strip().rstrip('?. ')}%",
            )
            return [dict(r) for r in rows]
    except asyncpg.exceptions.UndefinedTableError:
        return []
