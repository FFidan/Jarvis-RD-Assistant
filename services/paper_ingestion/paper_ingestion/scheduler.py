"""Automated fetch->embed pipeline scheduler for paper_ingestion."""

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from paper_ingestion.pipelines.auto_fetch import run_auto_pipeline
from paper_ingestion.recommender import refresh_recommendations

logger = logging.getLogger(__name__)

# Re-export so callers that do ``from paper_ingestion.scheduler import run_auto_pipeline``
# (e.g. tests) continue to work without modification.
__all__ = ["run_auto_pipeline", "run_pulse_wrapper", "start_scheduler"]


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
        # Local import to keep heavy paper_ingestion.pulse.* off the scheduler import path
        from paper_ingestion.pulse.job import run_pulse

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
