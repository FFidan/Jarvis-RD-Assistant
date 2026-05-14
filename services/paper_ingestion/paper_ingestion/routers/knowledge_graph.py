"""Knowledge graph endpoints.

Entity extraction, graph queries, and entity management.
"""

import logging
import uuid

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import assert_paper_ownership, current_user_id_or_none
from jarvis_common.auth import require_admin

from paper_ingestion.deps import (
    get_db_pool,
    get_http_client,
    get_optional_embedder,
    get_optional_qdrant,
    limiter,
)
from paper_ingestion.extraction.entities import (
    extract_entities_for_paper,
    get_knowledge_graph,
    query_knowledge_graph,
)
from paper_ingestion.models import (
    BatchEntityExtractResponse,
    EntityDetailResponse,
    EntityExtractionResponse,
    EntityResponse,
    KGQueryResponse,
    KnowledgeGraphResponse,
    RelationshipResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["knowledge-graph"])

VALID_ENTITY_TYPES = {"method", "dataset", "metric", "author", "institution", "concept"}


@router.post("/extract-entities/{paper_id}", response_model=EntityExtractionResponse)
@limiter.limit("5/minute")
async def extract_entities(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    embedder=Depends(get_optional_embedder),
    qdrant=Depends(get_optional_qdrant),
) -> EntityExtractionResponse:
    """Trigger entity extraction for a single paper."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    try:
        return await extract_entities_for_paper(
            http_client,
            db_pool,
            paper_id,
            embedder=embedder,
            qdrant_client=qdrant,
        )
    except ValueError as e:
        msg = str(e)
        # "Paper X not found" is a genuine 404; other ValueErrors (e.g.
        # "no chunks found") indicate bad input → 400.
        status = 404 if "not found" in msg.lower() else 400
        request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
        logger.exception(
            "Entity extraction failed for paper %d", paper_id, extra={"request_id": request_id}
        )
        raise HTTPException(
            status,
            detail=(
                f"Entity extraction failed (request_id={request_id})."
                " Please retry or contact support."
            ),
        ) from e


@router.post(
    "/extract-entities/batch",
    response_model=BatchEntityExtractResponse,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("2/minute")
async def batch_extract_entities(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    embedder=Depends(get_optional_embedder),
    qdrant=Depends(get_optional_qdrant),
) -> dict[str, int]:
    """Backfill entity extraction for all summarized papers."""
    async with db_pool.acquire() as conn:
        # Get papers with summaries but no entities
        rows = await conn.fetch(
            """SELECT p.id FROM papers p
               JOIN paper_summaries ps ON p.id = ps.paper_id
               WHERE p.id NOT IN (SELECT DISTINCT paper_id FROM paper_entities)
               ORDER BY p.created_at DESC
               LIMIT 50""",
        )

    extracted = 0
    failed = 0
    for row in rows:
        try:
            await extract_entities_for_paper(
                http_client,
                db_pool,
                row["id"],
                embedder=embedder,
                qdrant_client=qdrant,
            )
            extracted += 1
        except Exception:
            logger.exception("Entity extraction failed for paper %d", row["id"])
            failed += 1

    return {"extracted": extracted, "failed": failed, "total": len(rows)}


@router.get("/knowledge-graph", response_model=KnowledgeGraphResponse)
@limiter.limit("60/minute")
async def get_graph(
    request: Request,
    entity_type: str | None = None,
    min_paper_count: int = Query(default=1, ge=1),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> KnowledgeGraphResponse:
    """Get the full knowledge graph or filtered subset."""
    if entity_type is not None and entity_type not in VALID_ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid entity_type: {entity_type}. Valid types: {sorted(VALID_ENTITY_TYPES)}",
        )

    async with db_pool.acquire() as conn:
        data = await get_knowledge_graph(conn, entity_type, min_paper_count)

    entities = [
        EntityResponse(
            id=e["id"],
            name=e["name"],
            canonical_name=e["canonical_name"],
            entity_type=e["entity_type"],
            description=e.get("description"),
            metadata=e.get("metadata") or {},
            paper_count=e.get("paper_count", 1),
            created_at=e.get("created_at"),
            display_size=e.get("display_size", 20),
        )
        for e in data.get("entities") or []
    ]

    relationships = [
        RelationshipResponse(
            id=r["id"],
            source_entity_id=r["source_entity_id"],
            target_entity_id=r["target_entity_id"],
            relationship_type=r["relationship_type"],
            paper_id=r.get("paper_id"),
            evidence_quote=r.get("evidence_quote"),
            confidence=r.get("confidence", 1.0),
            created_at=r.get("created_at"),
        )
        for r in data.get("relationships") or []
    ]

    return KnowledgeGraphResponse(
        entities=entities,
        relationships=relationships,
        entity_type_counts=data.get("entity_type_counts") or {},
    )


@router.get("/knowledge-graph/entities", response_model=list[EntityResponse])
@limiter.limit("60/minute")
async def list_entities(
    request: Request,
    entity_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[EntityResponse]:
    """List entities with pagination and filtering."""
    if entity_type is not None and entity_type not in VALID_ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid entity_type: {entity_type}. Valid types: {sorted(VALID_ENTITY_TYPES)}",
        )

    async with db_pool.acquire() as conn:
        if entity_type:
            rows = await conn.fetch(
                """SELECT * FROM entities WHERE entity_type = $1
                   ORDER BY paper_count DESC LIMIT $2 OFFSET $3""",
                entity_type,
                limit,
                offset,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM entities ORDER BY paper_count DESC LIMIT $1 OFFSET $2",
                limit,
                offset,
            )

    return [
        EntityResponse(
            id=r["id"],
            name=r["name"],
            canonical_name=r["canonical_name"],
            entity_type=r["entity_type"],
            description=r.get("description"),
            metadata=r.get("metadata") or {},
            paper_count=r.get("paper_count", 1),
            created_at=r.get("created_at"),
        )
        for r in rows
    ]


@router.get("/knowledge-graph/entity/{entity_id}", response_model=EntityDetailResponse)
@limiter.limit("60/minute")
async def get_entity_detail(
    request: Request,
    entity_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> EntityDetailResponse:
    """Get entity detail with relationships and papers."""
    async with db_pool.acquire() as conn:
        entity = await conn.fetchrow("SELECT * FROM entities WHERE id = $1", entity_id)
        if not entity:
            raise HTTPException(404, f"Entity {entity_id} not found")

        rels = await conn.fetch(
            """SELECT * FROM entity_relationships
               WHERE source_entity_id = $1 OR target_entity_id = $1
               ORDER BY confidence DESC""",
            entity_id,
        )

        papers = await conn.fetch(
            """SELECT p.id, p.title, pe.mention_count
               FROM paper_entities pe
               JOIN papers p ON p.id = pe.paper_id
               WHERE pe.entity_id = $1
               ORDER BY pe.mention_count DESC""",
            entity_id,
        )

    return EntityDetailResponse(
        entity=EntityResponse(
            id=entity["id"],
            name=entity["name"],
            canonical_name=entity["canonical_name"],
            entity_type=entity["entity_type"],
            description=entity.get("description"),
            metadata=entity.get("metadata") or {},
            paper_count=entity.get("paper_count", 1),
            created_at=entity.get("created_at"),
        ),
        relationships=[
            RelationshipResponse(
                id=r["id"],
                source_entity_id=r["source_entity_id"],
                target_entity_id=r["target_entity_id"],
                relationship_type=r["relationship_type"],
                paper_id=r.get("paper_id"),
                evidence_quote=r.get("evidence_quote"),
                confidence=r.get("confidence", 1.0),
                created_at=r.get("created_at"),
            )
            for r in rels
        ],
        papers=[dict(p) for p in papers],
    )


@router.get("/knowledge-graph/query", response_model=KGQueryResponse)
@limiter.limit("10/minute")
async def kg_query(
    request: Request,
    q: str = Query(..., min_length=1),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> KGQueryResponse:
    """Query the knowledge graph with natural language."""
    async with db_pool.acquire() as conn:
        results = await query_knowledge_graph(conn, q)
    return KGQueryResponse(results=results, query=q)
