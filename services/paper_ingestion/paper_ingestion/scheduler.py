"""Automated fetch->embed pipeline scheduler for paper_ingestion."""

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from paper_ingestion.ingestion import refresh_recommendations
from paper_ingestion.pipelines.auto_fetch import run_auto_pipeline

logger = logging.getLogger(__name__)

# Re-export so callers that do ``from paper_ingestion.scheduler import run_auto_pipeline``
# (e.g. tests) continue to work without modification.
__all__ = [
    "run_auto_pipeline",
    "run_pulse_classifier_training_wrapper",
    "run_pulse_wrapper",
    "run_weekly_digest_wrapper",
    "run_zotero_sync_wrapper",
    "start_scheduler",
]


_DEFAULT_PULSE_CRON = "0 4 * * *"
_DEFAULT_PULSE_CLASSIFIER_CRON = "30 3 * * *"
_DEFAULT_WEEKLY_DIGEST_CRON = "0 9 * * 1"  # Monday 09:00
_DEFAULT_ZOTERO_CRON = "0 * * * *"  # hourly


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


async def _get_zotero_poll_config(db_pool: Any) -> tuple[bool, str]:
    """Return (poll_enabled, cron_expr) from user_config.

    Defaults: poll_enabled=False, cron='0 * * * *' (hourly).
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM user_config WHERE key IN"
                " ('zotero.poll_enabled', 'zotero.poll_cron')"
            )
    except Exception:
        logger.exception("zotero: failed to read zotero config")
        return False, _DEFAULT_ZOTERO_CRON

    cfg: dict[str, Any] = {}
    for row in rows:
        cfg[row["key"]] = row["value"]

    # Poll is gated solely on zotero.poll_enabled.
    poll_enabled = bool(cfg.get("zotero.poll_enabled", False))
    if not poll_enabled:
        return False, _DEFAULT_ZOTERO_CRON

    # Validate cron expression.
    raw_cron = cfg.get("zotero.poll_cron")
    cron_expr = _DEFAULT_ZOTERO_CRON
    if isinstance(raw_cron, str) and raw_cron.strip():
        expr = raw_cron.strip()
        try:
            CronTrigger.from_crontab(expr)
            cron_expr = expr
        except Exception:
            logger.warning(
                "zotero.poll_cron value %r is not a valid cron expression; using default", expr
            )
    return True, cron_expr


async def run_zotero_sync_wrapper(app: Any) -> None:
    """APScheduler entrypoint for Zotero library sync — defers via procrastinate."""
    db_pool = app.state.db_pool
    poll_enabled, _ = await _get_zotero_poll_config(db_pool)
    if not poll_enabled:
        logger.info("zotero: poll disabled via user_config, skipping scheduled sync")
        return
    try:
        import uuid  # noqa: PLC0415

        from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

        jarvis_job_id = str(uuid.uuid4())
        await KIND_TO_TASK["zotero.sync_from_zotero"].defer_async(
            job_id=jarvis_job_id, user_id=None
        )
        logger.info(
            "zotero: deferred zotero.sync_from_zotero job %s via procrastinate",
            jarvis_job_id,
        )
    except Exception:
        logger.exception("zotero: failed to defer sync job")


async def run_pulse_wrapper(app: Any) -> None:
    """APScheduler entrypoint — gated on ``pulse.enabled`` config."""
    db_pool = app.state.db_pool
    if not await _is_pulse_enabled(db_pool):
        logger.info("pulse: disabled via user_config, skipping nightly run")
        return
    try:
        import uuid  # noqa: PLC0415

        from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

        jarvis_job_id = str(uuid.uuid4())
        await KIND_TO_TASK["pulse.generate"].defer_async(job_id=jarvis_job_id, user_id=None)
        logger.info(
            "pulse: deferred pulse.generate job %s via procrastinate",
            jarvis_job_id,
        )
    except Exception:
        logger.exception("pulse: failed to defer pulse.generate job")


async def run_pulse_classifier_training_wrapper(app: Any) -> None:
    """APScheduler entrypoint for Pulse classifier retraining."""
    db_pool = app.state.db_pool
    if not await _is_pulse_enabled(db_pool):
        logger.info("pulse: disabled via user_config, skipping classifier retraining")
        return
    try:
        import uuid  # noqa: PLC0415

        from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

        jarvis_job_id = str(uuid.uuid4())
        await KIND_TO_TASK["pulse.train_classifier"].defer_async(job_id=jarvis_job_id, user_id=None)
        logger.info(
            "pulse: deferred pulse.train_classifier job %s via procrastinate",
            jarvis_job_id,
        )
    except Exception:
        logger.exception("pulse: failed to defer classifier training job")


async def run_weekly_digest_wrapper(app: Any) -> None:
    """APScheduler entrypoint for weekly digest regeneration.

    B.4 Step 3 canary (spec ``docs/specs/2026-05-03-b4-job-broker.md``):
    enqueues via the procrastinate ``digest.weekly`` task instead of the
    legacy ``jobs`` table. The legacy ``@job_handler("digest.weekly")``
    decorator on ``_digest_weekly_job`` remains in place — procrastinate
    dispatches into it via the registry shim. The JARVIS UUID is generated
    upfront and passed as the ``job_id`` kwarg so the SSE bridge can locate
    the procrastinate row via ``args->>'job_id'``.
    """
    import uuid  # noqa: PLC0415

    _ = app.state.db_pool  # touch to surface AttributeError early in tests
    try:
        from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

        jarvis_job_id = str(uuid.uuid4())
        await KIND_TO_TASK["digest.weekly"].defer_async(job_id=jarvis_job_id, days=7)
        logger.info("digest: deferred digest.weekly job %s via procrastinate", jarvis_job_id)
    except Exception:
        logger.exception("digest: failed to defer weekly digest job")


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

    # Pulse classifier training (cron-scheduled before the overnight deck; gated on pulse.enabled)
    try:
        scheduler.add_job(
            run_pulse_classifier_training_wrapper,
            trigger=CronTrigger.from_crontab(_DEFAULT_PULSE_CLASSIFIER_CRON),
            args=[app],
            id="pulse_classifier_training",
            name="Pulse classifier retraining",
            replace_existing=True,
            max_instances=1,
        )
        logger.info(
            "pulse_classifier_training scheduler registered (cron=%s)",
            _DEFAULT_PULSE_CLASSIFIER_CRON,
        )
    except Exception:
        logger.exception("Failed to register pulse_classifier_training job")

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

    # Weekly digest regeneration (cron-scheduled; GET /api/digest/weekly remains synchronous)
    try:
        scheduler.add_job(
            run_weekly_digest_wrapper,
            trigger=CronTrigger.from_crontab(_DEFAULT_WEEKLY_DIGEST_CRON),
            args=[app],
            id="weekly_digest",
            name="Weekly digest regeneration",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("weekly_digest scheduler registered (cron=%s)", _DEFAULT_WEEKLY_DIGEST_CRON)
    except Exception:
        logger.exception("Failed to register weekly_digest job")

    # Zotero library sync (cron-scheduled, gated on zotero.poll_enabled)
    try:
        _zotero_enabled, zotero_cron = await _get_zotero_poll_config(app.state.db_pool)
        scheduler.add_job(
            run_zotero_sync_wrapper,
            trigger=CronTrigger.from_crontab(zotero_cron),
            args=[app],
            id="zotero_library_sync",
            name="Zotero library sync",
            replace_existing=True,
            max_instances=1,
        )
        logger.info(
            "zotero_library_sync scheduler registered (cron=%s, enabled=%s)",
            zotero_cron,
            _zotero_enabled,
        )
    except Exception:
        logger.exception("Failed to register zotero_library_sync job")

    scheduler.start()
    logger.info("auto_pipeline scheduler started (interval=%.2fh)", interval_hours)
    return scheduler
