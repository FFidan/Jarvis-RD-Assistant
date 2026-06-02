"""Card generation endpoints (single-paper and batch).

Both POST /api/generate and POST /api/generate/batch now enqueue DB-backed
jobs handled by the jobs worker wired in main.py lifespan.  The old
in-memory ``app.jobs`` module is no longer used by this router.

The card-generation business logic and job handlers live in
``learning_engine.generation_service`` (no FastAPI deps); this router only
validates ownership and enqueues the jobs.
"""

import uuid

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from jarvis_common.auth import current_user_id_strict
from jarvis_common.db_helpers import assert_paper_ownership
from jarvis_common.task_registry import KIND_TO_TASK

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    BatchAcceptedResponse,
    BatchGenerateRequest,
    GenerateCardsRequest,
)

router = APIRouter(prefix="/api/generate", tags=["generation"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=202, response_model=BatchAcceptedResponse)
@limiter.limit("5/minute")
async def generate_cards(
    request: Request,
    body: GenerateCardsRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> BatchAcceptedResponse:
    """Enqueue card generation for a single paper; returns 202 with *job_id*."""
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, body.paper_id, user_id)
        deck = await conn.fetchval(
            "SELECT id FROM decks WHERE id = $1 AND user_id = $2",
            body.deck_id,
            user_id,
        )
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["card.generate"].defer_async(
        job_id=jarvis_job_id,
        user_id=user_id,
        paper_id=body.paper_id,
        deck_id=body.deck_id,
        max_cards=body.max_cards,
    )
    return BatchAcceptedResponse(job_id=jarvis_job_id, status="queued")


@router.post("/batch", status_code=202, response_model=BatchAcceptedResponse)
@limiter.limit("2/minute")
async def batch_generate_cards(
    request: Request,
    body: BatchGenerateRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> BatchAcceptedResponse:
    """Enqueue batch card generation; returns 202 immediately with a *job_id* to poll."""
    async with db_pool.acquire() as conn:
        deck = await conn.fetchval(
            "SELECT id FROM decks WHERE id = $1 AND user_id = $2",
            body.deck_id,
            user_id,
        )
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["card.generate_batch"].defer_async(
        job_id=jarvis_job_id,
        user_id=user_id,
        deck_id=body.deck_id,
        max_per_paper=body.max_per_paper,
    )
    return BatchAcceptedResponse(job_id=jarvis_job_id, status="queued")


# NOTE: GET /api/generate/batch/{job_id} has been removed (LE-004).
# Use GET /api/jobs/{job_id} instead — it has ownership checks and is the canonical
# job-status endpoint.
