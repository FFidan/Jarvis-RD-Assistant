"""Topic CRUD endpoints."""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import delete_or_404, dynamic_update, log_audit
from jarvis_common.auth import current_user_id_strict, require_admin

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import TopicCreate, TopicResponse, TopicUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/topics", tags=["topics"])

_TOPIC_ALLOWED_COLUMNS: set[str] = {"name", "query_terms", "enabled", "category", "description"}


@router.get("", response_model=list[TopicResponse])
@limiter.limit("60/minute")
async def list_topics(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[TopicResponse]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM topics ORDER BY name")
    return [TopicResponse(**dict(r)) for r in rows]


# Subscription routes declared BEFORE /{topic_id} to avoid shadowing by the
# parameterised route.
@router.get("/subscriptions", response_model=list[int])
@limiter.limit("60/minute")
async def list_my_subscriptions(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[int]:
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT topic_id FROM user_topic_subscriptions WHERE user_id = $1 ORDER BY topic_id",
            user_id,
        )
    return [int(r["topic_id"]) for r in rows]


@router.put("/{topic_id}/subscribe", status_code=204)
@limiter.limit("30/minute")
async def subscribe_to_topic(
    request: Request,
    topic_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> None:
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM topics WHERE id = $1", topic_id)
        if not exists:
            raise HTTPException(status_code=404, detail=f"Topic {topic_id} not found")
        await conn.execute(
            "INSERT INTO user_topic_subscriptions (user_id, topic_id) "
            "VALUES ($1, $2) ON CONFLICT (user_id, topic_id) DO NOTHING",
            user_id,
            topic_id,
        )


@router.delete("/{topic_id}/subscribe", status_code=204)
@limiter.limit("30/minute")
async def unsubscribe_from_topic(
    request: Request,
    topic_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> None:
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_topic_subscriptions WHERE user_id = $1 AND topic_id = $2",
            user_id,
            topic_id,
        )


@router.post(
    "",
    response_model=TopicResponse,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("30/minute")
async def create_topic(
    request: Request,
    body: TopicCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> TopicResponse:
    async with db_pool.acquire() as conn:
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


@router.put(
    "/{topic_id}",
    response_model=TopicResponse,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("30/minute")
async def update_topic(
    request: Request,
    topic_id: int,
    body: TopicUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> TopicResponse:
    async with db_pool.acquire() as conn:
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


@router.delete(
    "/{topic_id}",
    status_code=204,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("30/minute")
async def delete_topic(
    request: Request,
    topic_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    async with db_pool.acquire() as conn:
        await delete_or_404(
            conn,
            "DELETE FROM topics WHERE id = $1",
            topic_id,
            detail=f"Topic {topic_id} not found",
        )
    await log_audit(
        db_pool,
        action="delete_topic",
        resource=f"topic:{topic_id}",
    )
