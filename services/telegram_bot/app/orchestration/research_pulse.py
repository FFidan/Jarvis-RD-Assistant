"""Research pulse workflow: fetch new papers for all enabled topics."""

import asyncio
import logging

import asyncpg
import httpx
from telegram import Bot

from app.config import BotConfig
from app.formatters import format_paper_card, truncate

logger = logging.getLogger(__name__)


async def _search_term(
    http_client: httpx.AsyncClient, config: "BotConfig", term: str
) -> list[dict]:
    """Search for a single term, return papers list or empty on error."""
    try:
        resp = await http_client.post(
            f"{config.paper_ingestion_url}/api/search",
            json={"query": term, "source": "arxiv", "max_results": 10},
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("Failed to search term '%s'", term)
        return []


async def run_research_pulse(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Fetch new papers for all enabled topics and send briefing.

    For each topic, searches for papers, downloads and processes new ones,
    and sends a summary to Telegram.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    db_pool : asyncpg.Pool
        Database connection pool.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    """
    topics = await db_pool.fetch("SELECT * FROM topics WHERE enabled = TRUE")
    if not topics:
        logger.info("No enabled topics found")
        return

    all_new_papers: list[dict] = []

    for topic in topics:
        topic_name = topic["name"]
        try:
            # Parallel search all terms
            search_tasks = [
                _search_term(http_client, config, term) for term in topic["query_terms"]
            ]
            term_results = await asyncio.gather(*search_tasks)

            # Flatten all papers from all terms
            all_papers: list[dict] = []
            for papers in term_results:
                all_papers.extend(papers)

            # Process papers sequentially (rate limits for download/process/summarize)
            for paper in all_papers:
                paper_id = paper.get("id")
                if not paper_id:
                    continue

                # Always link paper to topic (idempotent, before notified check)
                await db_pool.execute(
                    """INSERT INTO paper_topics (paper_id, topic_id)
                    VALUES ($1, $2) ON CONFLICT DO NOTHING""",
                    paper_id,
                    topic["id"],
                )

                # Check if already notified — skip processing but not linking
                state = await db_pool.fetchrow(
                    "SELECT notified_at FROM paper_user_state WHERE paper_id = $1",
                    paper_id,
                )
                if state and state["notified_at"]:
                    continue

                # Try to process the paper
                processed_ok = True
                try:
                    resp = await http_client.post(
                        f"{config.paper_ingestion_url}/api/download-pdf/{paper_id}",
                        timeout=120.0,
                    )
                    resp.raise_for_status()
                    resp = await http_client.post(
                        f"{config.paper_ingestion_url}/api/process-pdf/{paper_id}",
                        timeout=180.0,
                    )
                    resp.raise_for_status()
                    resp = await http_client.post(
                        f"{config.paper_ingestion_url}/api/summarize/{paper_id}",
                        timeout=120.0,
                    )
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.warning("Failed to process paper %s, will retry next run", paper_id)
                    processed_ok = False

                # Compute relevance score (attempt even on failure)
                try:
                    await http_client.post(
                        f"{config.paper_ingestion_url}/api/relevance-score",
                        params={"paper_id": paper_id, "topic_id": topic["id"]},
                    )
                except httpx.HTTPError:
                    logger.debug("Failed to compute relevance score for paper %s", paper_id)

                # Only add to notification list on successful processing
                if processed_ok:
                    paper["topic_name"] = topic_name
                    all_new_papers.append(paper)
        except Exception:  # noqa: BLE001 — per-topic catch-all; must not abort other topics
            logger.exception("Failed to process topic '%s'", topic_name)

    if not all_new_papers:
        logger.info("No new papers found across all topics")
        return

    # Send briefing
    lines = [f"\U0001f4e1 <b>Research Pulse: {len(all_new_papers)} new papers</b>\n"]
    for paper in all_new_papers[:10]:
        lines.append(format_paper_card(paper))
        lines.append("")

    if len(all_new_papers) > 10:
        lines.append(f"<i>... and {len(all_new_papers) - 10} more</i>")

    message = truncate("\n".join(lines))
    try:
        await bot.send_message(chat_id=config.telegram_chat_id, text=message, parse_mode="HTML")
    except Exception:  # noqa: BLE001 — top-level send; must not crash the scheduler
        logger.exception("Failed to send message")
        return

    # Mark papers as notified only AFTER successful send
    for paper in all_new_papers:
        paper_id = paper.get("id")
        if paper_id:
            await db_pool.execute(
                """INSERT INTO paper_user_state (paper_id, notified_at)
                VALUES ($1, NOW())
                ON CONFLICT (paper_id) DO UPDATE SET notified_at = NOW()""",
                paper_id,
            )
    logger.info("Research pulse sent: %d papers", len(all_new_papers))
