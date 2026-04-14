"""Topic CRUD endpoints."""

import logging

from fastapi import APIRouter, HTTPException, Request
from jarvis_common import delete_or_404, dynamic_update

from app.deps import limiter
from app.models import TopicCreate, TopicResponse, TopicUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/topics", tags=["topics"])

_TOPIC_ALLOWED_COLUMNS: set[str] = {"name", "query_terms", "enabled", "category", "description"}


@router.get("", response_model=list[TopicResponse])
@limiter.limit("60/minute")
async def list_topics(request: Request) -> list[TopicResponse]:
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM topics ORDER BY name")
    return [TopicResponse(**dict(r)) for r in rows]


@router.post("", response_model=TopicResponse, status_code=201)
@limiter.limit("30/minute")
async def create_topic(request: Request, body: TopicCreate) -> TopicResponse:
    async with request.app.state.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO topics (name, query_terms, category, description, enabled)
            VALUES ($1, $2, $3, $4, $5) RETURNING *""",
            body.name,
            body.query_terms,
            body.category,
            body.description,
            body.enabled,
        )
    return TopicResponse(**dict(row))


@router.put("/{topic_id}", response_model=TopicResponse)
@limiter.limit("30/minute")
async def update_topic(request: Request, topic_id: int, body: TopicUpdate) -> TopicResponse:
    async with request.app.state.db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM topics WHERE id = $1", topic_id)
        if not existing:
            raise HTTPException(404, f"Topic {topic_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_TOPIC_ALLOWED_COLUMNS)
        if not updates:
            return TopicResponse(**dict(existing))

        row = await dynamic_update(
            conn,
            "topics",
            topic_id,
            updates,
            _TOPIC_ALLOWED_COLUMNS,
        )
    return TopicResponse(**dict(row))


@router.delete("/{topic_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_topic(request: Request, topic_id: int) -> None:
    async with request.app.state.db_pool.acquire() as conn:
        await delete_or_404(
            conn,
            "DELETE FROM topics WHERE id = $1",
            topic_id,
            detail=f"Topic {topic_id} not found",
        )
