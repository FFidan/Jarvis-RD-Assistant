"""Auto-fetch pipeline: discover → download → embed/process.

Extracted from ``paper_ingestion.scheduler`` so the scheduler module stays thin.
Called by the APScheduler job registered in ``scheduler.start_scheduler``.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from jarvis_common.library import fan_out_to_topic_users

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.models import PaperSourceConfig
from paper_ingestion.pdf_processor import PDF_STORAGE_PATH, check_pdf_path_safe
from paper_ingestion.services.pdf_workflow import run_process_pdf, upsert_paper
from paper_ingestion.sources.registry import get_source_class

logger = logging.getLogger(__name__)


def _resolve_topic_pairs(topics_rows) -> list[tuple[int | None, str]]:
    """Coerce raw ``topics`` rows into ``(topic_id, name)`` pairs.

    Keep the topic id alongside the name so a search-result paper can be
    fanned out to users subscribed to that topic via user_library.
    Defensive: tolerate fixtures / partial-schema rows that omit ``id``.
    Nameless rows are dropped; a non-int/str id coerces to ``None``; an
    empty result falls back to a single sensible default.
    """

    def _row_get(row: object, key: str) -> object | None:
        try:
            return row[key]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return None

    topic_pairs: list[tuple[int | None, str]] = []
    for row in topics_rows:
        name = _row_get(row, "name")
        if not name:
            continue
        tid: Any = _row_get(row, "id")
        topic_id = int(tid) if isinstance(tid, int | str) else None
        topic_pairs.append((topic_id, str(name)))
    if not topic_pairs:
        topic_pairs = [(None, "machine learning")]  # sensible default
    return topic_pairs


async def _discover_and_save(app, db_pool, sources_rows, topic_pairs) -> int:
    """For each enabled source: search per topic and save results.

    Returns the number of newly-inserted canonical papers. Each saved paper
    is fanned out (idempotently) to users subscribed to the matching topic.
    """
    papers_added = 0
    for src_row in sources_rows:
        source_type = src_row["source_type"]
        try:
            source_class = get_source_class(source_type)
            if source_class is None:
                logger.warning("auto_pipeline: unknown source %s, skipping", source_type)
                continue
            config = PaperSourceConfig(
                id=src_row["id"],
                source_type=src_row["source_type"],
                enabled=src_row["enabled"],
                config=src_row["config"] or {},
            )
            source = source_class(config, app.state.http_client)
            for topic_id, topic_name in topic_pairs:
                try:
                    results = await source.search(topic_name, max_results=20)
                    if results:
                        # batch save via internal function (bypasses HTTP rate limiter)
                        async with db_pool.acquire() as conn:
                            for paper in results:
                                try:
                                    # system-initiated bulk discovery
                                    paper.discovery_origin = "recommender"
                                    row = await upsert_paper(conn, paper)
                                    if row and row["is_insert"]:
                                        papers_added += 1
                                    # Fan out to every user subscribed to
                                    # this topic. Idempotent via
                                    # ON CONFLICT DO NOTHING.
                                    if row and topic_id is not None:
                                        try:
                                            await fan_out_to_topic_users(
                                                conn,
                                                paper_id=row["id"],
                                                topic_ids=[topic_id],
                                            )
                                        except Exception as fan_exc:
                                            logger.warning(
                                                "auto_pipeline: fan-out failed "
                                                "for paper %d topic %d: %s",
                                                row["id"],
                                                topic_id,
                                                fan_exc,
                                                exc_info=True,
                                            )
                                except Exception as e:
                                    logger.warning(
                                        "auto_pipeline: failed to save paper: %s",
                                        e,
                                        exc_info=True,
                                    )
                except Exception as e:
                    logger.warning(
                        "auto_pipeline: source %s topic '%s' failed: %s",
                        source_type,
                        topic_name,
                        e,
                        exc_info=True,
                    )
        except Exception as e:
            logger.error("auto_pipeline: source %s failed: %s", source_type, e, exc_info=True)

    return papers_added


async def _download_pending_pdfs(app, db_pool, sem) -> None:
    """Download PDFs for papers that have a pdf_url but no local PDF yet.

    ``sem`` is the function-local concurrency limiter created per
    ``run_auto_pipeline`` invocation and passed in here.
    """
    pdf_processor = app.state.pdf_processor

    async with db_pool.acquire() as conn:
        to_download = await conn.fetch(
            """SELECT id, pdf_url FROM papers
               WHERE pdf_downloaded = FALSE
                 AND pdf_local_path IS NULL
                 AND pdf_url IS NOT NULL
               LIMIT 20"""
        )
    logger.info("auto_pipeline: %d papers to download", len(to_download))

    async def _download_and_store_pdf(paper_id: int, pdf_url: str) -> None:
        async with sem:
            try:
                pdf_path = await pdf_processor.download_pdf(pdf_url, paper_id)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE papers SET pdf_local_path = $1,"
                        " pdf_downloaded = TRUE WHERE id = $2",
                        str(pdf_path),
                        paper_id,
                    )
                logger.info("auto_pipeline: downloaded PDF for paper %d", paper_id)
            except Exception as exc:
                logger.warning(
                    "auto_pipeline: failed to download PDF for paper %d: %s",
                    paper_id,
                    exc,
                    exc_info=True,
                )

    download_tasks = [
        asyncio.create_task(_download_and_store_pdf(row["id"], row["pdf_url"]))
        for row in to_download
    ]
    if download_tasks:
        # return_exceptions: a single failing task must not abort siblings.
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("auto_pipeline: download task failed: %s", r, exc_info=r)


async def _process_pending_papers(app, db_pool, sem) -> None:
    """Process papers that have a PDF but haven't been chunked/embedded yet.

    ``sem`` is the function-local concurrency limiter created per
    ``run_auto_pipeline`` invocation and passed in here.
    """
    pdf_processor = app.state.pdf_processor
    embedder = app.state.embedder

    async with db_pool.acquire() as conn:
        to_process = await conn.fetch(
            """SELECT p.id, p.pdf_local_path FROM papers p
               WHERE p.pdf_downloaded = TRUE
                 AND p.pdf_local_path IS NOT NULL
                 AND p.chunked_at IS NULL
               ORDER BY p.id
               LIMIT 20"""
        )
    logger.info("auto_pipeline: %d papers to process", len(to_process))

    async def _extract_and_embed_paper(paper_id: int, pdf_path: Path) -> None:
        async with sem:
            try:
                await run_process_pdf(
                    paper_id,
                    pdf_path,
                    db_pool,
                    pdf_processor,
                    embedder,
                    force=False,
                )
                logger.info("auto_pipeline: processed paper %d", paper_id)
            except Exception as exc:
                logger.warning(
                    "auto_pipeline: failed to process paper %d: %s",
                    paper_id,
                    exc,
                    exc_info=True,
                )

    process_tasks = []
    for row in to_process:
        pdf_path = Path(row["pdf_local_path"])
        if not check_pdf_path_safe(pdf_path, PDF_STORAGE_PATH):
            logger.warning(
                "Skipping paper %d: pdf_local_path outside storage dir",
                row["id"],
            )
            continue
        process_tasks.append(asyncio.create_task(_extract_and_embed_paper(row["id"], pdf_path)))
    if process_tasks:
        # return_exceptions: a single failing task must not abort siblings.
        results = await asyncio.gather(*process_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("auto_pipeline: process task failed: %s", r, exc_info=r)


async def run_auto_pipeline(app) -> None:
    """Fetch new papers from enabled sources, download PDFs, and process them.

    Self-gates when ``AUTO_FETCH_INTERVAL_HOURS`` is 0 (or unset), which
    happens when the scheduler is running but the user has disabled auto-fetch.
    """
    _interval = get_paper_ingestion_settings().auto_fetch_interval_hours
    if _interval <= 0:
        logger.debug("auto_pipeline: interval_hours=0, skipping run")
        return

    db_pool = app.state.db_pool
    sem = asyncio.Semaphore(3)  # cap concurrent embedding tasks; leaves headroom for HTTP requests

    logger.info("auto_pipeline: starting run")
    try:
        # 1. Query enabled sources and topics
        async with db_pool.acquire() as conn:
            sources_rows = await conn.fetch(
                "SELECT * FROM paper_sources WHERE enabled = TRUE"
                " ORDER BY display_order ASC, id ASC"
            )
            topics_rows = await conn.fetch("SELECT id, name FROM topics")

        topic_pairs = _resolve_topic_pairs(topics_rows)

        # 2. For each enabled source: search per topic and save results
        papers_added = await _discover_and_save(app, db_pool, sources_rows, topic_pairs)
        logger.info("auto_pipeline: saved %d papers", papers_added)

        # 3. Trigger batch processing (extract, embed, summarize) for unprocessed papers
        # 3a. Download PDFs for papers that have a pdf_url but no local PDF yet
        await _download_pending_pdfs(app, db_pool, sem)

        # 3b. Process papers that have a PDF but haven't been chunked/embedded yet
        await _process_pending_papers(app, db_pool, sem)

        logger.info("auto_pipeline: run complete")

    except Exception as e:
        logger.error("auto_pipeline: unhandled error: %s", e, exc_info=True)
