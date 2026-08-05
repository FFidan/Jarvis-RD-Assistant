"""Auto-fetch pipeline: discover → download → embed/process.

Extracted from ``paper_ingestion.scheduler`` so the scheduler module stays thin.
Called by the APScheduler job registered in ``scheduler.start_scheduler``.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis_common.config_flags import read_global_config_flag
from jarvis_common.library import fan_out_to_topic_users
from jarvis_common.maintenance import skip_for_maintenance

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME
from paper_ingestion.models import PaperSourceConfig, TopicRef
from paper_ingestion.pdf_processor import PDF_STORAGE_PATH, resolve_safe_pdf_path
from paper_ingestion.services.pdf_workflow import (
    download_and_store_pdf,
    reclaim_discarded_paper_content,
    run_process_pdf,
    upsert_verified_public_paper,
)
from paper_ingestion.sources.registry import get_source_class

logger = logging.getLogger(__name__)

# Discovery lookback window — mirrors pulse.profile.PulseProfile.lookback_days' default.
_DISCOVERY_LOOKBACK_DAYS = 7
_AUTO_PROCESS_PAGE_SIZE = 20
_AUTO_PROCESS_CONCURRENCY = 3
# Non-persistent placeholder id for the topic-less default query pair
# ((None, "machine learning", []) from _resolve_topic_pairs). TopicRef.id stays
# strict `int` (shared with pulse scoring's dampened-topic matching), so this
# fills the field for a query that has no real topic. Safe because Postgres
# serial ids start at 1 (never collides with a real row) and this value is
# only read by PaperSource.fetch_new_since()/_parts_for_topic() (name/
# query_terms only) — never persisted, fanned out, or scored.
_DEFAULT_QUERY_TOPIC_ID = 0


# Projection feeding _resolve_topic_pairs. query_terms is what a source
# actually searches for, so dropping it from the projection silently degrades
# every discovery query to the bare topic name.
_DISCOVERY_TOPICS_SQL = "SELECT id, name, query_terms FROM topics"

# System-scoped ``user_config`` key holding the last successful pipeline run.
# The scheduler keeps jobs in memory only, so a fire due while the service was
# down is lost; this stamp is what lets the next boot notice and catch up.
AUTO_PIPELINE_LAST_RUN_KEY = "scheduler.auto_pipeline.last_run"

_LAST_RUN_UPSERT_SQL = (
    "INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb) "
    "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
)


def _resolve_topic_pairs(topics_rows) -> list[tuple[int | None, str, list[str]]]:
    """Coerce raw ``topics`` rows into ``(topic_id, name, query_terms)`` triples.

    Keep the topic id alongside the name so a search-result paper can be
    fanned out to users subscribed to that topic via user_library.
    Defensive: tolerate fixtures / partial-schema rows that omit ``id`` or
    ``query_terms``. Nameless rows are dropped; a non-int/str id coerces to
    ``None``; a missing, ``None``, or non-sequence ``query_terms`` coerces
    to an empty list (a present sequence has its elements stringified); an
    empty result falls back to a single sensible default.
    """

    def _row_get(row: object, key: str) -> object | None:
        try:
            return row[key]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return None

    topic_pairs: list[tuple[int | None, str, list[str]]] = []
    for row in topics_rows:
        name = _row_get(row, "name")
        if not name:
            continue
        tid: Any = _row_get(row, "id")
        topic_id = int(tid) if isinstance(tid, int | str) else None
        raw_terms = _row_get(row, "query_terms")
        query_terms = [str(t) for t in raw_terms] if isinstance(raw_terms, list | tuple) else []
        topic_pairs.append((topic_id, str(name), query_terms))
    if not topic_pairs:
        topic_pairs = [(None, "machine learning", [])]  # sensible default
    return topic_pairs


async def _discover_and_save(app, db_pool, sources_rows, topic_pairs) -> int:
    """For each enabled source: fetch papers new since the lookback window, per topic.

    Returns the number of newly-inserted canonical papers. Each saved paper
    is fanned out (idempotently) to users subscribed to the matching topic.
    A topic-less pair (topic_id is None — the zero-configured-topics default)
    still fetches and saves papers (landing PUBLIC, visible to all) but is
    never fanned out: there is no subscribed topic to target.
    """
    papers_added = 0
    since = datetime.now(UTC) - timedelta(days=_DISCOVERY_LOOKBACK_DAYS)
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
            source = source_class(config, app.state.http_client, db_pool=db_pool)
            for topic_id, topic_name, topic_query_terms in topic_pairs:
                try:
                    results = await source.fetch_new_since(
                        since,
                        [
                            TopicRef(
                                id=topic_id if topic_id is not None else _DEFAULT_QUERY_TOPIC_ID,
                                name=topic_name,
                                query_terms=topic_query_terms,
                            )
                        ],
                        limit=20,
                    )
                    if results:
                        # batch save via internal function (bypasses HTTP rate limiter)
                        discarded_content_ids: list[int] = []
                        async with db_pool.acquire() as conn:
                            for paper in results:
                                try:
                                    # system-initiated bulk discovery
                                    paper.discovery_origin = "recommender"
                                    row = await upsert_verified_public_paper(
                                        conn,
                                        paper,
                                        discarded_content_ids=discarded_content_ids,
                                    )
                                    if row and row["is_insert"]:
                                        papers_added += 1
                                    # Fan out to every user subscribed to
                                    # this topic. Idempotent via
                                    # ON CONFLICT DO NOTHING. Skipped for the
                                    # topic-less fallback (topic_id is None):
                                    # there is no subscribed topic to target.
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
                        # Outside the acquired connection: reclaiming storage is
                        # network and disk work that must not hold a pool slot.
                        for paper_id in discarded_content_ids:
                            await reclaim_discarded_paper_content(paper_id, db_pool)
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
                await download_and_store_pdf(db_pool, pdf_processor, pdf_url, paper_id)
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


async def _is_auto_summarize_enabled(db_pool: Any) -> bool:
    """Read ``user_config['automation.auto_summarize_discovered']`` — defaults to False."""
    return await read_global_config_flag(
        db_pool, "automation.auto_summarize_discovered", log_label="auto_pipeline"
    )


_UNSUMMARIZED_HOLDERS_SQL = """
    SELECT ul.user_id FROM user_library ul
    WHERE ul.paper_id = $1
      AND NOT EXISTS (
          SELECT 1 FROM paper_summaries s
          WHERE s.paper_id = ul.paper_id AND s.user_id = ul.user_id
            AND s.content_generation = (
                SELECT content_generation FROM papers WHERE id = ul.paper_id
            )
      )
"""


async def _maybe_defer_summarize(db_pool, paper_id: int) -> None:
    """Best-effort defer ``paper.summarize`` once per library holder lacking a summary.

    Summaries are per-user by schema and every reader binds a strict integer
    owner, so a NULL-owned summary is unreadable. A paper nobody holds gets
    no summary at all.
    """
    async with db_pool.acquire() as conn:
        holders = await conn.fetch(_UNSUMMARIZED_HOLDERS_SQL, paper_id)
    if not holders:
        return
    try:
        from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

        for row in holders:
            await KIND_TO_TASK["paper.summarize"].defer_async(
                job_id=str(uuid.uuid4()), user_id=row["user_id"], paper_id=paper_id
            )
    except Exception:
        logger.exception("paper.summarize enqueue failed for paper %d", paper_id)


async def _process_pending_papers(app, db_pool, sem) -> None:
    """Process incomplete PDFs and reconcile a bounded page of completed papers.

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
               ORDER BY CASE
                          WHEN p.chunked_at IS NULL OR EXISTS (
                              SELECT 1 FROM paper_chunks c
                               WHERE c.paper_id = p.id
                                 AND (c.embedding_model IS DISTINCT FROM $1
                                      OR c.embedding_id IS NULL)
                          ) THEN 0 ELSE 1
                        END,
                        p.chunked_at NULLS FIRST,
                        p.id
               LIMIT $2""",
            EMBEDDING_MODEL_NAME,
            _AUTO_PROCESS_PAGE_SIZE,
        )
    logger.info("auto_pipeline: %d papers to process", len(to_process))
    auto_summarize_enabled = await _is_auto_summarize_enabled(db_pool)

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
                if auto_summarize_enabled:
                    await _maybe_defer_summarize(db_pool, paper_id)
            except Exception as exc:
                logger.warning(
                    "auto_pipeline: failed to process paper %d: %s",
                    paper_id,
                    exc,
                    exc_info=True,
                )

    process_tasks = []
    for row in to_process:
        # require_exists=False: this site historically only guarded against
        # path traversal, not disk presence -- a missing file surfaces later
        # via _extract_and_embed_paper's own try/except instead.
        pdf_path = resolve_safe_pdf_path(
            row["pdf_local_path"], PDF_STORAGE_PATH, require_exists=False
        )
        if pdf_path is None:
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
    if skip_for_maintenance("auto pipeline"):
        return
    _interval = get_paper_ingestion_settings().auto_fetch_interval_hours
    if _interval <= 0:
        logger.debug("auto_pipeline: interval_hours=0, skipping run")
        return

    db_pool = app.state.db_pool
    sem = asyncio.Semaphore(
        _AUTO_PROCESS_CONCURRENCY
    )  # leaves headroom for interactive HTTP requests

    logger.info("auto_pipeline: starting run")
    try:
        # 1. Query enabled sources and topics
        async with db_pool.acquire() as conn:
            sources_rows = await conn.fetch(
                "SELECT * FROM paper_sources WHERE enabled = TRUE"
                " ORDER BY display_order ASC, id ASC"
            )
            topics_rows = await conn.fetch(_DISCOVERY_TOPICS_SQL)

        topic_pairs = _resolve_topic_pairs(topics_rows)

        # 2. For each enabled source: fetch new-since results per topic and save
        papers_added = await _discover_and_save(app, db_pool, sources_rows, topic_pairs)
        logger.info("auto_pipeline: saved %d papers", papers_added)

        # 3. Trigger batch processing (extract, embed, summarize) for unprocessed papers
        # 3a. Download PDFs for papers that have a pdf_url but no local PDF yet
        await _download_pending_pdfs(app, db_pool, sem)

        # 3b. Process papers that have a PDF but haven't been chunked/embedded yet
        await _process_pending_papers(app, db_pool, sem)

        logger.info("auto_pipeline: run complete")

        # Only a run that reached this point counts: a failed run leaves the
        # stamp where it was so the next boot still schedules a catch-up.
        async with db_pool.acquire() as conn:
            await conn.execute(
                _LAST_RUN_UPSERT_SQL,
                AUTO_PIPELINE_LAST_RUN_KEY,
                datetime.now(UTC).isoformat(),
            )

    except Exception as e:
        logger.error("auto_pipeline: unhandled error: %s", e, exc_info=True)
