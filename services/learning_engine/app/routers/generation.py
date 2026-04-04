"""Card generation endpoints (single-paper and batch)."""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import get_smart_model

from app.card_store import insert_card as _insert_card
from app.card_generator import CardGenerator
from app.converters import row_to_card_response
from app.deps import get_card_generator, get_db_pool, get_fsrs_manager, limiter
from app.fsrs_manager import FSRSManager
from app.models import (
    BatchGenerateRequest,
    BatchGenerateResponse,
    CardResponse,
    GenerateCardsRequest,
    GenerateCardsResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["generation"])


@router.post("/api/generate", response_model=GenerateCardsResponse)
@limiter.limit("5/minute")
async def generate_cards(
    request: Request,
    body: GenerateCardsRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    fsrs_manager: FSRSManager = Depends(get_fsrs_manager),
    card_generator: CardGenerator = Depends(get_card_generator),
) -> GenerateCardsResponse:
    """Generate flashcards from a paper using LLM with quote verification."""
    async with db_pool.acquire() as conn:
        # Validate deck exists
        deck = await conn.fetchval("SELECT id FROM decks WHERE id = $1", body.deck_id)
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")

        # Fetch paper metadata
        paper = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", body.paper_id)
        if not paper:
            raise HTTPException(status_code=404, detail="Paper not found")

        # Fetch chunks
        chunk_rows = await conn.fetch(
            "SELECT id, content, page_number FROM paper_chunks"
            " WHERE paper_id = $1 ORDER BY chunk_index",
            body.paper_id,
        )
        if not chunk_rows:
            raise HTTPException(
                status_code=400,
                detail="Paper has no processed chunks. Run process-pdf first.",
            )

        # Read configured model from user_config (falls back to "smart")
        smart_model = get_smart_model()

    chunks = [dict(row) for row in chunk_rows]

    try:
        result = await card_generator.generate_cards(
            title=paper["title"],
            authors=paper["authors"],
            chunks=chunks,
            paper_id=body.paper_id,
            abstract=paper.get("abstract"),
            max_cards=body.max_cards,
            model=smart_model,
        )
    except Exception as exc:
        logger.exception("Card generation failed")
        raise HTTPException(status_code=502, detail="Card generation failed") from exc

    verified_cards = result["cards"]

    # Insert verified cards into DB
    created: list[CardResponse] = []
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for card_data in verified_cards:
                fsrs_state, due_at = fsrs_manager.create_new_card()
                try:
                    row = await _insert_card(
                        conn,
                        body.deck_id,
                        body.paper_id,
                        card_data["card_type"],
                        card_data["front"],
                        card_data["back"],
                        card_data["evidence"],
                        fsrs_state,
                        due_at,
                    )
                except asyncpg.ForeignKeyViolationError as exc:
                    raise HTTPException(status_code=404, detail="Deck or paper not found") from exc
                created.append(row_to_card_response(row))

    return GenerateCardsResponse(
        cards_created=len(created),
        cards=created,
        confidence=result.get("confidence", "LOW"),
    )


@router.post("/api/generate/batch", response_model=BatchGenerateResponse)
@limiter.limit("2/minute")
async def batch_generate_cards(
    request: Request,
    body: BatchGenerateRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    fsrs_manager: FSRSManager = Depends(get_fsrs_manager),
    card_generator: CardGenerator = Depends(get_card_generator),
) -> BatchGenerateResponse:
    """Generate flashcards for all papers that have chunks but no cards yet for a given deck."""
    async with db_pool.acquire() as conn:
        # Preflight: verify deck exists
        deck = await conn.fetchval("SELECT id FROM decks WHERE id = $1", body.deck_id)
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")

        # Read configured model from user_config
        smart_model = get_smart_model()

        # Query papers with chunks but no cards yet for this deck (limit 50)
        paper_rows = await conn.fetch(
            """
            SELECT p.id FROM papers p
            WHERE EXISTS (SELECT 1 FROM paper_chunks WHERE paper_id = p.id)
              AND NOT EXISTS (SELECT 1 FROM cards WHERE paper_id = p.id AND deck_id = $1)
            LIMIT 50
            """,
            body.deck_id,
        )

    papers_processed = 0
    cards_created = 0
    errors: list[str] = []

    for paper_row in paper_rows:
        paper_id = paper_row["id"]
        try:
            async with db_pool.acquire() as conn:
                paper = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
                chunk_rows = await conn.fetch(
                    "SELECT id, content, page_number FROM paper_chunks"
                    " WHERE paper_id = $1 ORDER BY chunk_index",
                    paper_id,
                )

            if not paper or not chunk_rows:
                errors.append(f"Paper {paper_id}: missing metadata or chunks")
                continue

            chunks = [dict(row) for row in chunk_rows]

            result = await card_generator.generate_cards(
                title=paper["title"],
                authors=paper["authors"],
                chunks=chunks,
                paper_id=paper_id,
                abstract=paper.get("abstract"),
                max_cards=body.max_per_paper,
                model=smart_model,
            )

            verified_cards = result["cards"]
            paper_cards_created = 0
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    for card_data in verified_cards:
                        fsrs_state, due_at = fsrs_manager.create_new_card()
                        try:
                            row = await _insert_card(
                                conn,
                                body.deck_id,
                                paper_id,
                                card_data["card_type"],
                                card_data["front"],
                                card_data["back"],
                                card_data["evidence"],
                                fsrs_state,
                                due_at,
                            )
                            if row:
                                paper_cards_created += 1
                        except asyncpg.ForeignKeyViolationError:
                            errors.append(f"Paper {paper_id}: FK violation on card insert")
                            raise  # abort this paper's transaction

            papers_processed += 1
            cards_created += paper_cards_created

        except asyncpg.ForeignKeyViolationError:
            continue  # already recorded in errors list by inner handler
        except Exception as exc:
            logger.exception("Batch generate failed for paper %d", paper_id)
            errors.append(f"Paper {paper_id}: {exc}")
        except BaseException:
            logger.exception("Batch generate aborted (BaseException) for paper %d", paper_id)
            raise

    return BatchGenerateResponse(
        papers_processed=papers_processed,
        cards_created=cards_created,
        errors=errors,
    )
