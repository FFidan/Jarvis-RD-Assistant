"""Card generation endpoints (single-paper and batch).

Both POST /api/generate and POST /api/generate/batch now enqueue DB-backed
jobs handled by the jobs worker wired in main.py lifespan.  The old
in-memory ``app.jobs`` module is no longer used by this router.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import get_smart_model
from jarvis_common import jobs as jobs_lib
from jarvis_common.jobs import JobContext, JobError, job_handler

from app.card_generator import CardGenerator
from app.card_store import insert_card as _insert_card
from app.converters import row_to_card_response
from app.deps import get_db_pool, limiter
from app.fsrs_manager import FSRSManager
from app.models import (
    BatchAcceptedResponse,
    BatchGenerateRequest,
    BatchGenerateResponse,
    CardResponse,
    GenerateCardsRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["generation"])


# ---------------------------------------------------------------------------
# Core generation helper (used by both job handlers and direct endpoint)
# ---------------------------------------------------------------------------


async def generate_cards_core(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    paper_id: int,
    deck_id: int,
    max_cards: int,
    fsrs_manager: FSRSManager | None = None,
    card_generator: CardGenerator | None = None,
    ctx: JobContext | None = None,
) -> dict[str, Any]:
    """Fetch chunks, call LLM, verify and insert cards.

    Parameters
    ----------
    pool:           asyncpg connection pool
    http_client:    shared httpx client (passed to CardGenerator if needed)
    paper_id:       paper to generate cards for
    deck_id:        target deck
    max_cards:      upper bound on generated cards
    fsrs_manager:   injected via FastAPI dep or created fresh inside job
    card_generator: injected via FastAPI dep or created fresh inside job
    ctx:            optional JobContext for progress reporting

    Returns
    -------
    dict with keys: cards_created (int), cards (list), confidence (str)
    """
    from jarvis_common.llm_client import LITELLM_FALLBACK_ENV_NAMES, get_litellm_config

    # Lazily create dependencies when running inside a job handler
    if fsrs_manager is None:
        fsrs_manager = FSRSManager()
    if card_generator is None:
        litellm_config = get_litellm_config(fallback_env_names=LITELLM_FALLBACK_ENV_NAMES)
        card_generator = CardGenerator(
            http_client=http_client,
            litellm_config=litellm_config,
        )

    if ctx:
        await ctx.update_progress(0.1, "Validating deck and paper")

    async with pool.acquire() as conn:
        deck = await conn.fetchval("SELECT id FROM decks WHERE id = $1", deck_id)
        if not deck:
            raise JobError("Deck not found")

        paper = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
        if not paper:
            raise JobError("Paper not found")

        if ctx:
            await ctx.update_progress(0.2, "Fetching chunks")

        chunk_rows = await conn.fetch(
            "SELECT id, content, page_number FROM paper_chunks"
            " WHERE paper_id = $1 ORDER BY chunk_index",
            paper_id,
        )

    if not chunk_rows:
        raise JobError(
            "Paper has no processed chunks",
            action_link={
                "label": "Process PDF now",
                "href": f"/paper/{paper_id}?action=process",
            },
        )

    if ctx:
        await ctx.update_progress(0.3, "Building prompt")

    chunks = [dict(row) for row in chunk_rows]
    smart_model = get_smart_model()

    if ctx:
        await ctx.update_progress(0.4, "Streaming generation")

    try:
        result = await card_generator.generate_cards(
            title=paper["title"],
            authors=paper["authors"],
            chunks=chunks,
            paper_id=paper_id,
            abstract=paper.get("abstract"),
            max_cards=max_cards,
            model=smart_model,
        )
    except (httpx.HTTPError, asyncpg.PostgresError, ValueError, RuntimeError) as exc:
        logger.exception("Card generation failed for paper %s", paper_id)
        raise RuntimeError("Card generation failed") from exc
    # JobError propagates naturally — carries action_link payload for the UI

    if ctx:
        await ctx.update_progress(0.85, "Verifying")

    verified_cards = result["cards"]
    created: list[CardResponse] = []

    async with pool.acquire() as conn:
        async with conn.transaction():
            for card_data in verified_cards:
                fsrs_state, due_at = fsrs_manager.create_new_card()
                try:
                    row = await _insert_card(
                        conn,
                        deck_id,
                        paper_id,
                        card_data["card_type"],
                        card_data["front"],
                        card_data["back"],
                        card_data["evidence"],
                        fsrs_state,
                        due_at,
                    )
                except asyncpg.ForeignKeyViolationError as exc:
                    raise JobError("Deck or paper not found") from exc
                created.append(row_to_card_response(row))

    if ctx:
        await ctx.update_progress(1.0, "Done")

    return {
        "cards_created": len(created),
        "cards": [c.model_dump() for c in created],
        "confidence": result.get("confidence", "LOW"),
    }


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


@job_handler("card.generate")
async def _card_generate_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for single-paper card generation."""
    return await generate_cards_core(
        pool=pool,
        http_client=http_client,
        paper_id=payload["paper_id"],
        deck_id=payload["deck_id"],
        max_cards=payload.get("max_cards", 5),
        ctx=ctx,
    )


@job_handler("card.generate_batch")
async def _card_generate_batch_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for batch card generation across all unprocessed papers in a deck."""
    deck_id: int = payload["deck_id"]
    max_per_paper: int = payload.get("max_per_paper", 5)

    async with pool.acquire() as conn:
        paper_rows = await conn.fetch(
            """
            SELECT p.id FROM papers p
            WHERE EXISTS (SELECT 1 FROM paper_chunks WHERE paper_id = p.id)
              AND NOT EXISTS (SELECT 1 FROM cards WHERE paper_id = p.id AND deck_id = $1)
            LIMIT 50
            """,
            deck_id,
        )

    total = len(paper_rows)
    papers_processed = 0
    cards_created = 0
    errors: list[str] = []

    for i, paper_row in enumerate(paper_rows):
        paper_id = paper_row["id"]
        if await ctx.is_cancelled():
            break

        await ctx.update_progress(i / max(total, 1), f"Paper {i + 1}/{total}")

        try:
            result = await generate_cards_core(
                pool=pool,
                http_client=http_client,
                paper_id=paper_id,
                deck_id=deck_id,
                max_cards=max_per_paper,
                ctx=None,
            )
            papers_processed += 1
            cards_created += result["cards_created"]
        except JobError as exc:
            # Paper has no chunks or not found — record but continue batch
            errors.append(f"Paper {paper_id}: {exc}")
        except Exception as exc:
            logger.exception("Batch generate failed for paper %s", paper_id)
            errors.append(f"Paper {paper_id}: {exc}")

    await ctx.update_progress(1.0, "Batch complete")

    return BatchGenerateResponse(
        papers_processed=papers_processed,
        cards_created=cards_created,
        errors=errors,
    ).model_dump()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/generate", status_code=202)
@limiter.limit("5/minute")
async def generate_cards(
    request: Request,
    body: GenerateCardsRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Enqueue card generation for a single paper; returns 202 with *job_id*."""
    job_id = await jobs_lib.enqueue(
        db_pool,
        "card.generate",
        payload={
            "paper_id": body.paper_id,
            "deck_id": body.deck_id,
            "max_cards": body.max_cards,
        },
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/api/generate/batch", status_code=202, response_model=BatchAcceptedResponse)
@limiter.limit("2/minute")
async def batch_generate_cards(
    request: Request,
    body: BatchGenerateRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> BatchAcceptedResponse:
    """Enqueue batch card generation; returns 202 immediately with a *job_id* to poll."""
    async with db_pool.acquire() as conn:
        deck = await conn.fetchval("SELECT id FROM decks WHERE id = $1", body.deck_id)
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")

    job_id = await jobs_lib.enqueue(
        db_pool,
        "card.generate_batch",
        payload={
            "deck_id": body.deck_id,
            "max_per_paper": body.max_per_paper,
        },
    )
    return BatchAcceptedResponse(job_id=job_id, status="queued")


@router.get("/api/generate/batch/{job_id}")
async def get_batch_status(
    job_id: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Poll the status of a batch generation job started by POST /api/generate/batch."""
    row = await jobs_lib.get(db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Normalise datetime fields for JSON serialisation
    out: dict[str, Any] = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
