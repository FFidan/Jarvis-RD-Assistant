"""Card-generation business logic and job handlers (no FastAPI deps).

This module owns the core card-generation helper plus the two procrastinate
job handlers (single-paper and batch).  It deliberately carries no FastAPI
router / Request dependencies — only the business logic and job context types —
so it can be imported by the worker registration path without dragging in the
HTTP layer.
"""

import logging
from typing import Any

import asyncpg
import httpx
import openai
from jarvis_common import effective_num_ctx, get_smart_model
from jarvis_common.db_helpers import assert_paper_ownership
from jarvis_common.jobs import JobError, ProgressContext

from learning_engine.card_generator import CardGenerator, _empty_result
from learning_engine.card_store import insert_card
from learning_engine.converters import row_to_card_response
from learning_engine.fsrs_manager import FSRSManager
from learning_engine.models import (
    CardResponse,
    _BatchGenerateResult,
)

logger = logging.getLogger(__name__)


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
    ctx: ProgressContext | None = None,
    *,
    user_id: int | None = None,
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
    ctx:            optional ProgressContext for progress reporting
    user_id:        per-user identity written to cards.user_id (None = single-tenant)

    Returns
    -------
    dict with keys: cards_created (int), cards (list), confidence (str)
    """
    from jarvis_common.llm_client import get_litellm_config

    from learning_engine._state import get_services  # noqa: PLC0415

    # Lazily create dependencies when running inside a job handler
    if fsrs_manager is None:
        fsrs_manager = FSRSManager()
    if card_generator is None:
        litellm_config = get_litellm_config()
        card_generator = CardGenerator(
            http_client=http_client,
            litellm_config=litellm_config,
        )

    openai_client = get_services().openai_client

    if ctx:
        await ctx.update_progress(0.1, "Validating deck and paper")

    async with pool.acquire() as conn:
        deck = await conn.fetchval(
            "SELECT id FROM decks WHERE id = $1 AND user_id = $2",
            deck_id,
            user_id,
        )
        if not deck:
            raise JobError("Deck not found")

        # Defense-in-depth: re-validate paper ownership even when called from
        # a job worker (RD-DA-001). assert_paper_ownership is a no-op when
        # user_id is None (internal/system dispatch paths).
        await assert_paper_ownership(conn, paper_id, user_id)  # type: ignore[arg-type]

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

        summary_row = await conn.fetchrow(
            "SELECT summary_detailed, methodology, limitations"
            " FROM paper_summaries WHERE paper_id = $1"
            " ORDER BY created_at DESC LIMIT 1",
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

    summary_text: str | None = None
    if summary_row:
        parts = [
            p
            for p in [
                summary_row["summary_detailed"],
                summary_row["methodology"],
                summary_row["limitations"],
            ]
            if p
        ]
        if parts:
            summary_text = "\n\n".join(parts)

    if ctx:
        await ctx.update_progress(0.4, "Streaming generation")

    if openai_client is None:
        raise RuntimeError(
            "openai_client not initialized — check _init_langfuse_hook ran during lifespan"
        )

    try:
        result = await card_generator.generate_cards(
            title=paper["title"],
            authors=paper["authors"],
            chunks=chunks,
            openai_client=openai_client,
            paper_id=paper_id,
            abstract=paper.get("abstract"),
            max_cards=max_cards,
            model=smart_model,
            summary_text=summary_text,
            num_ctx=await effective_num_ctx(pool, "smart"),
        )
    except openai.APIStatusError as exc:
        logger.error("Provider HTTP error during card generation for paper %s: %s", paper_id, exc)
        result = _empty_result(reason="llm_error")
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
                    row = await insert_card(
                        conn,
                        deck_id,
                        paper_id,
                        card_data["card_type"],
                        card_data["front"],
                        card_data["back"],
                        card_data["evidence"],
                        fsrs_state,
                        due_at,
                        user_id=user_id,
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


async def _card_generate_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Job handler for single-paper card generation."""
    return await generate_cards_core(
        pool=pool,
        http_client=http_client,
        paper_id=payload["paper_id"],
        deck_id=payload["deck_id"],
        max_cards=payload.get("max_cards", 5),
        ctx=ctx,
        user_id=payload.get("user_id"),
    )


async def _card_generate_batch_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Job handler for batch card generation across all unprocessed papers in a deck."""
    deck_id: int = payload["deck_id"]
    max_per_paper: int = payload.get("max_per_paper", 5)
    user_id: int | None = payload.get("user_id")

    async with pool.acquire() as conn:
        if user_id is not None:
            paper_rows = await conn.fetch(
                """
                SELECT p.id FROM papers p
                WHERE EXISTS (SELECT 1 FROM paper_chunks WHERE paper_id = p.id)
                  AND NOT EXISTS (SELECT 1 FROM cards WHERE paper_id = p.id AND deck_id = $1)
                  AND EXISTS (
                    SELECT 1 FROM user_library ul
                    WHERE ul.paper_id = p.id AND ul.user_id IS NOT DISTINCT FROM $2
                  )
                LIMIT 50
                """,
                deck_id,
                user_id,
            )
        else:
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
                user_id=user_id,
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

    return _BatchGenerateResult(
        papers_processed=papers_processed,
        cards_created=cards_created,
        errors=errors,
    ).model_dump()
