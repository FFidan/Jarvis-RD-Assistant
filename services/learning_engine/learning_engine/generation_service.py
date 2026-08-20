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
from jarvis_common.db_helpers import assert_paper_ownership, lock_paper_content_generation
from jarvis_common.jobs import JobError, ProgressContext, batch_terminal_status

from learning_engine.card_generator import CardGenerator, _empty_result
from learning_engine.card_store import insert_card
from learning_engine.converters import row_to_card_response
from learning_engine.fsrs_manager import FSRSManager
from learning_engine.models import (
    CardResponse,
    _BatchGenerateResult,
)

logger = logging.getLogger(__name__)

# Papers one batch-generation run may process. Each one costs a model call, so
# a large deck is covered over several runs rather than in a single job.
_BATCH_PAPER_LIMIT = 50

# A paper is eligible when it has chunks, has no current card in the deck, and —
# when the job belongs to a user — is in that user's library. The predicate is
# shared so the page and its count can never drift apart.
_ELIGIBLE_PAPERS_FROM = """
    FROM papers p
    WHERE EXISTS (SELECT 1 FROM paper_chunks WHERE paper_id = p.id)
      AND NOT EXISTS (
        SELECT 1 FROM cards c
        WHERE c.paper_id = p.id AND c.deck_id = $1
          AND c.content_generation = p.content_generation
      )
      AND ($2::bigint IS NULL OR EXISTS (
        SELECT 1 FROM user_library ul
        WHERE ul.paper_id = p.id AND ul.user_id IS NOT DISTINCT FROM $2
      ))
"""
_ELIGIBLE_PAPERS_SQL = f"SELECT p.id {_ELIGIBLE_PAPERS_FROM} LIMIT {_BATCH_PAPER_LIMIT}"
_ELIGIBLE_PAPERS_COUNT_SQL = f"SELECT count(*)::int {_ELIGIBLE_PAPERS_FROM}"


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
        async with conn.transaction():
            deck = await conn.fetchval(
                "SELECT id FROM decks WHERE id = $1 AND user_id = $2",
                deck_id,
                user_id,
            )
            if not deck:
                raise JobError("Deck not found")

            # Defense-in-depth: re-validate paper access even when called from
            # a job worker. Internal single-user dispatches may carry no owner.
            await assert_paper_ownership(conn, paper_id, user_id)  # type: ignore[arg-type]

            # Capture one source generation with the chunks and admit only a
            # summary derived from that source. The post-inference generation
            # check rejects a replacement that splits this input snapshot.
            paper = await conn.fetchrow(
                """
                SELECT p.*, ps.summary_detailed, ps.methodology, ps.limitations
                FROM papers p
                LEFT JOIN paper_summaries ps
                  ON ps.paper_id = p.id
                 AND ps.user_id IS NOT DISTINCT FROM $2
                 AND ps.content_generation = p.content_generation
                WHERE p.id = $1
                """,
                paper_id,
                user_id,
            )
            if not paper:
                raise JobError("Paper not found")
            content_generation = int(paper["content_generation"])

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

    summary_text: str | None = None
    if paper["summary_detailed"] or paper["methodology"] or paper["limitations"]:
        parts = [
            p
            for p in [
                paper["summary_detailed"],
                paper["methodology"],
                paper["limitations"],
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
            await lock_paper_content_generation(conn, paper_id)
            current_generation = await conn.fetchval(
                "SELECT content_generation FROM papers WHERE id = $1",
                paper_id,
            )
            if current_generation is None:
                raise JobError("Paper not found")
            if int(current_generation) != content_generation:
                raise JobError("Paper content changed during card generation; please retry")
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
                        content_generation=content_generation,
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
    """Job handler for batch card generation across unprocessed papers in a deck.

    One run covers at most :data:`_BATCH_PAPER_LIMIT` papers. The result reports
    the deck's whole eligible count and what this run left behind, so a deck
    larger than one run never reads as finished.
    """
    deck_id: int = payload["deck_id"]
    max_per_paper: int = payload.get("max_per_paper", 5)
    user_id: int | None = payload.get("user_id")

    async with pool.acquire() as conn:
        paper_rows = await conn.fetch(_ELIGIBLE_PAPERS_SQL, deck_id, user_id)
        # A short page is the whole eligible set, so only a full one needs
        # counting to tell the user how much of the deck is still waiting.
        eligible_total = (
            int(await conn.fetchval(_ELIGIBLE_PAPERS_COUNT_SQL, deck_id, user_id))
            if len(paper_rows) == _BATCH_PAPER_LIMIT
            else len(paper_rows)
        )

    batch_size = len(paper_rows)
    papers_processed = 0
    cards_created = 0
    errors: list[str] = []
    cancelled = False

    for i, paper_row in enumerate(paper_rows):
        paper_id = paper_row["id"]
        if await ctx.is_cancelled():
            cancelled = True
            break

        await ctx.update_progress(i / max(batch_size, 1), f"Paper {i + 1}/{batch_size}")

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
        except Exception:
            # The errors list is returned to the client; keep the detail in the
            # log and surface only a static message.
            logger.exception("Batch generate failed for paper %s", paper_id)
            errors.append(f"Paper {paper_id}: card generation failed")

    remaining = max(eligible_total - papers_processed - len(errors), 0)
    status = batch_terminal_status(cancelled=cancelled, incomplete=bool(errors) or remaining > 0)
    headline = "Done" if status == "ok" else status.title()
    await ctx.update_progress(
        1.0,
        f"{headline}: {papers_processed}/{eligible_total} processed, {cards_created} cards created",
    )

    # Status, deck total, and what is left describe the run and the deck rather
    # than the cards produced, so they are layered onto the result dict instead
    # of onto _BatchGenerateResult: that model is shared with the single-paper
    # path, which has no batch to be cancelled part-way through.
    batch_result = _BatchGenerateResult(
        papers_processed=papers_processed,
        cards_created=cards_created,
        errors=errors,
    ).model_dump()
    batch_result["status"] = status
    batch_result["total"] = eligible_total
    batch_result["remaining"] = remaining
    return batch_result
