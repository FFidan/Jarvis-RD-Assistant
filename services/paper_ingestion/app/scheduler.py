"""Automated fetch->embed pipeline scheduler for paper_ingestion."""

import asyncio
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.recommender import refresh_recommendations

logger = logging.getLogger(__name__)


async def run_auto_pipeline(app) -> None:
    """Fetch new papers from enabled sources, download PDFs, and process them.

    Self-gates when ``AUTO_FETCH_INTERVAL_HOURS`` is 0 (or unset), which
    happens when the scheduler is running but the user has disabled auto-fetch.
    """
    import os as _os

    _interval = float(_os.environ.get("AUTO_FETCH_INTERVAL_HOURS", "0"))
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
            topics_rows = await conn.fetch("SELECT name FROM topics")

        topics = [row["name"] for row in topics_rows]
        if not topics:
            topics = ["machine learning"]  # sensible default

        # 2. For each enabled source: search per topic and save results
        from .models import PaperSourceConfig
        from .services.pdf_workflow import upsert_paper  # local imports to avoid circular
        from .sources.registry import get_source_class

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
                for topic in topics:
                    try:
                        results = await source.search(topic, max_results=20)
                        if results:
                            # batch save via internal function (bypasses HTTP rate limiter)
                            async with db_pool.acquire() as conn:
                                for paper in results:
                                    try:
                                        row = await upsert_paper(conn, paper)
                                        if row and row["is_insert"]:
                                            papers_added += 1
                                    except Exception as e:
                                        logger.warning("auto_pipeline: failed to save paper: %s", e)
                    except Exception as e:
                        logger.warning(
                            "auto_pipeline: source %s topic '%s' failed: %s",
                            source_type,
                            topic,
                            e,
                        )
            except Exception as e:
                logger.error("auto_pipeline: source %s failed: %s", source_type, e, exc_info=True)

        logger.info("auto_pipeline: saved %d papers", papers_added)

        # 3. Trigger batch processing (extract, embed, summarize) for unprocessed papers
        # Use semaphore to limit concurrency
        # Deferred import to break circular dependency: main.py imports scheduler for setup
        from pathlib import Path

        from .pdf_processor import PDF_STORAGE_PATH
        from .services.pdf_workflow import run_process_pdf

        pdf_processor = app.state.pdf_processor
        embedder = app.state.embedder

        # 3a. Download PDFs for papers that have a pdf_url but no local PDF yet
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
                    )

        download_tasks = [
            asyncio.create_task(_download_and_store_pdf(row["id"], row["pdf_url"]))
            for row in to_download
        ]
        if download_tasks:
            await asyncio.gather(*download_tasks)

        # 3b. Process papers that have a PDF but haven't been chunked/embedded yet
        async with db_pool.acquire() as conn:
            to_process = await conn.fetch(
                """SELECT p.id, p.pdf_local_path FROM papers p
                   WHERE p.pdf_downloaded = TRUE
                     AND p.pdf_local_path IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id
                     )
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
                    logger.warning("auto_pipeline: failed to process paper %d: %s", paper_id, exc)

        storage_resolved = Path(PDF_STORAGE_PATH).resolve()
        process_tasks = []
        for row in to_process:
            pdf_path = Path(row["pdf_local_path"])
            if not pdf_path.resolve().is_relative_to(storage_resolved):
                logger.warning(
                    "Skipping paper %d: pdf_local_path outside storage dir",
                    row["id"],
                )
                continue
            process_tasks.append(asyncio.create_task(_extract_and_embed_paper(row["id"], pdf_path)))
        if process_tasks:
            await asyncio.gather(*process_tasks)

        logger.info("auto_pipeline: run complete")

    except Exception as e:
        logger.error("auto_pipeline: unhandled error: %s", e, exc_info=True)


_DEFAULT_PULSE_CRON = "0 4 * * *"


async def _is_pulse_enabled(db_pool: Any) -> bool:
    """Read ``user_config['pulse.enabled']`` — defaults to False if missing."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key = 'pulse.enabled'")
    except Exception:
        logger.exception("pulse: failed to read pulse.enabled config")
        return False
    if row is None:
        return False
    # asyncpg JSONB auto-decodes — value may be bool directly
    value = row["value"]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


async def _get_pulse_cron(db_pool: Any) -> str:
    """Read ``user_config['pulse.cron']`` — defaults to ``'0 4 * * *'``."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key = 'pulse.cron'")
    except Exception:
        logger.exception("pulse: failed to read pulse.cron config")
        return _DEFAULT_PULSE_CRON
    if row is None or row["value"] is None:
        return _DEFAULT_PULSE_CRON
    value = row["value"]
    if isinstance(value, str) and value.strip():
        expr = value.strip()
        try:
            CronTrigger.from_crontab(expr)
        except Exception:
            logger.warning(
                "pulse.cron value %r is not a valid cron expression; falling back to default",
                expr,
            )
            return _DEFAULT_PULSE_CRON
        return expr
    return _DEFAULT_PULSE_CRON


async def run_pulse_wrapper(app: Any) -> None:
    """APScheduler entrypoint — gated on ``pulse.enabled`` config."""
    db_pool = app.state.db_pool
    if not await _is_pulse_enabled(db_pool):
        logger.info("pulse: disabled via user_config, skipping nightly run")
        return
    try:
        # Local import to keep heavy app.pulse.* off the scheduler import path
        from app.pulse.job import run_pulse

        await run_pulse(
            db_pool=db_pool,
            http_client=app.state.http_client,
            embedder=app.state.embedder,
            source_cache=getattr(app.state, "sources", None),
        )
    except Exception:
        logger.exception("pulse_overnight job failed")


async def start_scheduler(app, interval_hours: float) -> AsyncIOScheduler:
    """Start the APScheduler and return the scheduler instance."""

    async def _run_recommendations(app: Any) -> None:
        try:
            count = await refresh_recommendations(app)
            logger.info("Nightly recommendations: %d saved", count)
        except Exception:
            logger.exception("Nightly recommendation refresh failed")

    scheduler = AsyncIOScheduler()

    # Register auto_pipeline unconditionally — the job self-gates when interval_hours <= 0.
    # This allows live-enabling via the Settings UI without restarting the service.
    _effective_interval = max(int(interval_hours), 1) if interval_hours > 0 else 24
    scheduler.add_job(
        run_auto_pipeline,
        trigger=IntervalTrigger(hours=_effective_interval),
        args=[app],
        id="auto_pipeline",
        name="Auto fetch->process pipeline",
        replace_existing=True,
        max_instances=1,  # prevent overlap if a run takes longer than the interval
    )
    scheduler.add_job(
        _run_recommendations,
        IntervalTrigger(hours=24),
        args=[app],
        id="recommendation_refresh",
        name="Nightly recommendation refresh",
        replace_existing=True,
        max_instances=1,
    )

    # Pulse overnight deck (cron-scheduled, gated on pulse.enabled)
    try:
        cron_expr = await _get_pulse_cron(app.state.db_pool)
        scheduler.add_job(
            run_pulse_wrapper,
            trigger=CronTrigger.from_crontab(cron_expr),
            args=[app],
            id="pulse_overnight",
            name="Overnight Pulse deck generation",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("pulse_overnight scheduler registered (cron=%s)", cron_expr)
    except Exception:
        logger.exception("Failed to register pulse_overnight job")

    scheduler.start()
    logger.info("auto_pipeline scheduler started (interval=%.2fh)", interval_hours)
    return scheduler
