"""Automated fetch->embed pipeline scheduler for paper_ingestion."""

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from jarvis_common.advisory_lock import _kind_lock_key
from jarvis_common.serialization import _coerce_bool

from paper_ingestion.ingestion import refresh_recommendations
from paper_ingestion.pipelines.auto_fetch import run_auto_pipeline

logger = logging.getLogger(__name__)

# Re-export so callers that do ``from paper_ingestion.scheduler import run_auto_pipeline``
# (e.g. tests) continue to work without modification.
__all__ = [
    "purge_system_events_task",
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
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = 'pulse.enabled' AND user_id IS NULL"
            )
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


async def _list_active_users(db_pool: Any) -> list[int]:
    """List active (non-deleted) user IDs for per-user cron fan-out.

    Schedulers iterate users-with-feature-enabled. ``user_config`` is
    still global (per-user keying deferred), so this helper currently
    returns all active users; once ``user_config`` becomes per-user the
    callers should narrow on ``key=feature.enabled AND value=true``.

    Returns an empty list when the ``users`` table is missing (single-tenant
    pre-migration-069 deployments) so callers fall back to the legacy
    system-shared single defer.
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM users WHERE deleted_at IS NULL ORDER BY id ASC")
        return [int(r["id"]) for r in rows]
    except Exception:
        logger.debug("scheduler: users table unreadable; falling back to system run", exc_info=True)
        return []


async def _users_without_active_pulse_lock(db_pool: Any, user_ids: list[int]) -> list[int]:
    """Return the subset of *user_ids* whose pulse advisory lock is not currently held.

    Probes ``pg_try_advisory_xact_lock`` (transaction-scoped, auto-released — leak-proof)
    for each user inside a short transaction so Postgres releases the lock automatically
    at transaction end.  No explicit unlock is needed; if the connection dies mid-loop
    the lock is never stranded on the pooled connection.
    Users whose lock is already held by a running pipeline are excluded.
    """
    key1 = _kind_lock_key("pulse.generate")
    free: list[int] = []
    async with db_pool.acquire() as conn:
        for uid in user_ids:
            async with conn.transaction():
                got = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1, $2)", key1, uid or 0
                )
            if got:
                free.append(uid)
    return free


def _has_config_value(cfg: dict[str, Any], key: str) -> bool:
    value = cfg.get(key)
    return value is not None and str(value).strip() != ""


async def _list_zotero_polling_users(db_pool: Any) -> list[int]:
    """List users whose personal Zotero config is ready for scheduled polling."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT u.id, c.key, c.value, c.encrypted_value
                FROM users u
                JOIN user_config c ON c.user_id = u.id
                WHERE u.deleted_at IS NULL
                  AND c.key = ANY($1::text[])
                ORDER BY u.id ASC
                """,
                [
                    "zotero.poll_enabled",
                    "zotero.api_key",
                    "zotero.user_id",
                    "zotero.library_type",
                    "zotero.group_id",
                ],
            )
    except Exception:
        logger.debug("scheduler: Zotero user_config unreadable; skipping scheduled poll")
        return []

    by_user: dict[int, dict[str, Any]] = {}
    for row in rows:
        user_id = int(row["id"])
        cfg = by_user.setdefault(user_id, {})
        if row["encrypted_value"] is not None:
            cfg[row["key"]] = "<encrypted>"
        else:
            cfg[row["key"]] = row["value"]

    ready: list[int] = []
    for user_id, cfg in by_user.items():
        if not _coerce_bool(cfg.get("zotero.poll_enabled")):
            continue
        if not _has_config_value(cfg, "zotero.api_key"):
            continue
        if not _has_config_value(cfg, "zotero.user_id"):
            continue
        if cfg.get("zotero.library_type") == "group" and not _has_config_value(
            cfg, "zotero.group_id"
        ):
            continue
        ready.append(user_id)
    return ready


async def _get_pulse_cron(db_pool: Any) -> str:
    """Read ``user_config['pulse.cron']`` — defaults to ``'0 4 * * *'``."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = 'pulse.cron' AND user_id IS NULL"
            )
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
                exc_info=True,
            )
            return _DEFAULT_PULSE_CRON
        return expr
    return _DEFAULT_PULSE_CRON


async def _get_zotero_poll_config(db_pool: Any) -> tuple[bool, str]:
    """Return scheduler registration readiness and cron_expr from user_config.

    The scheduler itself is always allowed to run. Per-user polling is gated by
    each user's ``zotero.poll_enabled`` row in ``_list_zotero_polling_users``.
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM user_config WHERE key IN"
                " ('zotero.poll_cron') AND user_id IS NULL"
            )
    except Exception:
        logger.exception("zotero: failed to read zotero config")
        return True, _DEFAULT_ZOTERO_CRON

    cfg: dict[str, Any] = {}
    for row in rows:
        cfg[row["key"]] = row["value"]

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
                "zotero.poll_cron value %r is not a valid cron expression; using default",
                expr,
                exc_info=True,
            )
    return True, cron_expr


async def _defer_per_user(
    *,
    task_kind: str,
    db_pool: Any,
    log_label: str,
    user_ids: list[int] | None = None,
    **task_kwargs: Any,
) -> int:
    """Iterate active users and defer one ``task_kind`` job per user.

    Returns 0 (no-op) when no active users exist. The legacy fallback to a
    single system-shared defer with ``user_id=None`` has been removed — a
    multi-user system with zero users has nothing to do, and ``user_id=None``
    defers leak unattributable work into the job table.
    """
    import uuid  # noqa: PLC0415

    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    user_ids = user_ids if user_ids is not None else await _list_active_users(db_pool)
    if not user_ids:
        logger.info(
            "%s: no active users — skipping %s deferral",
            log_label,
            task_kind,
        )
        return 0
    deferred = 0
    for uid in user_ids:
        try:
            jarvis_job_id = str(uuid.uuid4())
            await KIND_TO_TASK[task_kind].defer_async(
                job_id=jarvis_job_id,
                user_id=uid,
                **task_kwargs,
            )
            deferred += 1
            logger.info(
                "%s: deferred %s job %s for user %d", log_label, task_kind, jarvis_job_id, uid
            )
        except Exception:
            logger.exception("%s: failed to defer %s for user %d", log_label, task_kind, uid)
    return deferred


async def run_zotero_sync_wrapper(app: Any) -> None:
    """APScheduler entrypoint for Zotero library sync — defers via procrastinate."""
    db_pool = app.state.db_pool
    try:
        user_ids = await _list_zotero_polling_users(db_pool)
        await _defer_per_user(
            task_kind="zotero.sync_from_zotero",
            db_pool=db_pool,
            log_label="zotero",
            user_ids=user_ids,
        )
    except Exception:
        logger.exception("zotero: failed to defer sync job")


async def run_pulse_wrapper(app: Any) -> None:
    """APScheduler entrypoint — gated on ``pulse.enabled`` config.

    Skips users whose pulse advisory lock is currently held (i.e. a
    ``/pulse_now`` or earlier cron job is still running) to prevent stacking
    duplicate pipeline runs.
    """
    db_pool = app.state.db_pool
    if not await _is_pulse_enabled(db_pool):
        logger.info("pulse: disabled via user_config, skipping nightly run")
        return
    try:
        user_ids = await _users_without_active_pulse_lock(
            db_pool, await _list_active_users(db_pool)
        )
        if not user_ids:
            logger.info("pulse: all active users have an in-flight run — skipping")
            return
        await _defer_per_user(
            task_kind="pulse.generate", db_pool=db_pool, log_label="pulse", user_ids=user_ids
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
        # Train per-user classifier.
        await _defer_per_user(
            task_kind="pulse.train_classifier", db_pool=db_pool, log_label="pulse"
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

    Fans out one digest job per active user instead of producing a single
    global digest.
    """
    db_pool = app.state.db_pool  # touch to surface AttributeError early in tests
    try:
        await _defer_per_user(
            task_kind="digest.weekly", db_pool=db_pool, log_label="digest", days=7
        )
    except Exception:
        logger.exception("digest: failed to defer weekly digest job")


async def purge_system_events_task(app: Any) -> None:
    """Tiered retention: 30 days for app events, 7 days for infra events."""
    from jarvis_common.event_log import log_event  # noqa: PLC0415

    pool = app.state.db_pool
    try:
        app_result = await pool.execute(
            "DELETE FROM system_events WHERE category != 'infra'"
            " AND created_at < NOW() - INTERVAL '30 days'"
        )
        infra_result = await pool.execute(
            "DELETE FROM system_events WHERE category = 'infra'"
            " AND created_at < NOW() - INTERVAL '7 days'"
        )

        # asyncpg.execute returns "DELETE <n>"; parse counts
        def _count(s: str) -> int:
            try:
                return int(s.split()[-1])
            except Exception:
                return -1

        app_n = _count(app_result)
        infra_n = _count(infra_result)
        await log_event(
            pool=pool,
            level="info",
            category="config",
            source="purge_system_events",
            message=f"deleted {app_n} app + {infra_n} infra events",
        )
    except Exception:
        logger.exception("purge_system_events: failed to purge old events")


async def start_scheduler(app, interval_hours: float) -> AsyncIOScheduler:
    """Start the APScheduler with all registered cron/interval jobs.

    Registers jobs for: auto-fetch pipeline, nightly recommendations, Pulse
    overnight deck, Pulse classifier retraining, weekly digest, Zotero sync,
    system-events purge, and user-deletion hard-purge.  Each job is gated by
    its own feature-flag check at run time so no service restart is required
    to enable/disable individual features via the web UI.

    Parameters
    ----------
    app : FastAPI
        Running FastAPI application instance; used to access ``app.state``
        (``db_pool``, ``sources``, etc.) from inside scheduled callbacks.
    interval_hours : float
        Requested auto-fetch interval.  0 registers the job at a 24 h interval
        but the job self-gates on the feature flag so it effectively becomes
        a no-op until enabled.

    Returns
    -------
    AsyncIOScheduler
        Started APScheduler instance.  The caller stores it on ``app.state``
        so it can be shut down during the FastAPI lifespan teardown.
    """

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

    # Tiered system_events purge (daily at 2 AM)
    try:
        scheduler.add_job(
            purge_system_events_task,
            trigger=CronTrigger.from_crontab("0 2 * * *"),
            args=[app],
            id="purge_system_events",
            name="Tiered system_events purge",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("purge_system_events scheduler registered (cron=0 2 * * *)")
    except Exception:
        logger.exception("Failed to register purge_system_events job")

    # Daily hard-purge of soft-deleted users past grace period.
    from paper_ingestion.jobs.data_purge import register_data_purge  # noqa: PLC0415

    register_data_purge(scheduler, app)

    # BE-09: daily purge of expired magic_link_tokens rows.
    from paper_ingestion.jobs.purge_tokens import register_purge_tokens  # noqa: PLC0415

    register_purge_tokens(scheduler, app)

    scheduler.start()
    logger.info("auto_pipeline scheduler started (interval=%.2fh)", interval_hours)
    return scheduler
