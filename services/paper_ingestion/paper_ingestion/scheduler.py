"""Automated fetch->embed pipeline scheduler for paper_ingestion."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from jarvis_common.advisory_lock import _kind_lock_key
from jarvis_common.maintenance import skip_for_maintenance
from jarvis_common.serialization import _coerce_bool, read_global_config_flag

from paper_ingestion.ingestion import refresh_recommendations
from paper_ingestion.pipelines.auto_fetch import AUTO_PIPELINE_LAST_RUN_KEY, run_auto_pipeline

logger = logging.getLogger(__name__)

# Interval triggers default their start_date to construction time, which re-phases
# every job to the moment of the last restart — a box that reboots daily never
# reaches a long interval. Anchoring to a fixed past instant keeps the fire grid
# stable across restarts.
_INTERVAL_EPOCH = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)

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
_DEFAULT_SYSTEM_EVENTS_PURGE_CRON = "0 2 * * *"
_DEFAULT_JOB_HISTORY_PURGE_CRON = "30 2 * * *"  # daily, after the system-events purge

# procrastinate registers its own maintenance tasks under the "builtin" namespace;
# this is the registry key of the one that deletes finished job rows.
_REMOVE_OLD_JOBS_TASK = "builtin:procrastinate.builtin_tasks.remove_old_jobs"
# Finished jobs stay visible for 30 days, so a job reclaimed as failed after an
# interrupted run is still readable long after the operator notices it.
_JOB_HISTORY_MAX_HOURS = 24 * 30
_ZOTERO_POLL_CONFIG_KEYS = [
    "zotero.poll_enabled",
    "zotero.api_key",
    "zotero.user_id",
    "zotero.library_type",
    "zotero.group_id",
    "zotero.poll_cron",
]


def _zotero_poll_job_id(user_id: int) -> str:
    """Return the APScheduler job id for one user's Zotero poll."""
    return f"zotero_library_sync_{user_id}"


async def _is_pulse_enabled(db_pool: Any) -> bool:
    """Read ``user_config['pulse.enabled']`` — defaults to False if missing."""
    return await read_global_config_flag(db_pool, "pulse.enabled", log_label="pulse")


async def _list_active_users(db_pool: Any) -> list[int] | None:
    """List active (non-deleted) user IDs for per-user cron fan-out.

    Schedulers iterate users-with-feature-enabled. ``user_config`` is
    still global (per-user keying deferred), so this helper currently
    returns all active users; once ``user_config`` becomes per-user the
    callers should narrow on ``key=feature.enabled AND value=true``.

    Returns ``None`` -- distinct from an empty list -- when the ``users``
    table could not be read (e.g. missing on single-tenant
    pre-migration-069 deployments, or a transient DB error), so callers can
    tell "genuinely zero active users" apart from "the read itself failed".
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM users WHERE deleted_at IS NULL ORDER BY id ASC")
        return [int(r["id"]) for r in rows]
    except Exception:
        logger.debug("scheduler: users table unreadable; falling back to system run", exc_info=True)
        return None


async def _users_without_active_lock(db_pool: Any, user_ids: list[int], *, kind: str) -> list[int]:
    """Return the subset of *user_ids* whose *kind* advisory lock is not currently held.

    Probes ``pg_try_advisory_xact_lock`` (transaction-scoped, auto-released — leak-proof)
    for each user inside a short transaction so Postgres releases the lock automatically
    at transaction end.  No explicit unlock is needed; if the connection dies mid-loop
    the lock is never stranded on the pooled connection.
    Users whose lock is already held by a running job are excluded.
    """
    key1 = _kind_lock_key(kind)
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


async def _fetch_zotero_poll_config_rows(db_pool: Any, user_id: int | None) -> list[Any]:
    """Fetch personal Zotero polling config rows, optionally for one user."""
    async with db_pool.acquire() as conn:
        sql = """
            SELECT u.id, c.key, c.value, c.encrypted_value
            FROM users u
            JOIN user_config c ON c.user_id = u.id
            WHERE u.deleted_at IS NULL
              AND c.key = ANY($1::text[])
        """
        args: list[Any] = [_ZOTERO_POLL_CONFIG_KEYS]
        if user_id is not None:
            sql += " AND u.id = $2"
            args.append(user_id)
        sql += " ORDER BY u.id ASC"
        return await conn.fetch(sql, *args)


def _zotero_rows_by_user(rows: list[Any]) -> dict[int, dict[str, Any]]:
    """Group user_config rows into a per-user config mapping."""
    by_user: dict[int, dict[str, Any]] = {}
    for row in rows:
        uid = int(row["id"])
        cfg = by_user.setdefault(uid, {})
        cfg[row["key"]] = "<encrypted>" if row["encrypted_value"] is not None else row["value"]
    return by_user


def _zotero_poll_config_ready(cfg: dict[str, Any]) -> bool:
    """Return whether a per-user Zotero config is ready for polling."""
    if not _coerce_bool(cfg.get("zotero.poll_enabled")):
        return False
    if not _has_config_value(cfg, "zotero.api_key"):
        return False
    if not _has_config_value(cfg, "zotero.user_id"):
        return False
    return not (
        cfg.get("zotero.library_type") == "group" and not _has_config_value(cfg, "zotero.group_id")
    )


def _zotero_poll_cron(cfg: dict[str, Any], user_id: int) -> str:
    """Return a validated per-user Zotero poll cron, falling back to default."""
    raw_cron = cfg.get("zotero.poll_cron")
    if not isinstance(raw_cron, str) or not raw_cron.strip():
        return _DEFAULT_ZOTERO_CRON
    expr = raw_cron.strip()
    try:
        CronTrigger.from_crontab(expr)
    except Exception:
        logger.warning(
            "zotero.poll_cron value %r for user %d is invalid; using default",
            expr,
            user_id,
            exc_info=True,
        )
        return _DEFAULT_ZOTERO_CRON
    return expr


async def _list_zotero_polling_schedules(
    db_pool: Any,
    *,
    user_id: int | None = None,
) -> list[tuple[int, str]]:
    """List ready Zotero polling users with their personal cron schedules."""
    rows = await _fetch_zotero_poll_config_rows(db_pool, user_id)
    return [
        (uid, _zotero_poll_cron(cfg, uid))
        for uid, cfg in _zotero_rows_by_user(rows).items()
        if _zotero_poll_config_ready(cfg)
    ]


async def _list_zotero_polling_users(db_pool: Any) -> list[int]:
    """List users whose personal Zotero config is ready for scheduled polling."""
    return [uid for uid, _cron in await _list_zotero_polling_schedules(db_pool)]


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


async def _read_auto_pipeline_last_run(db_pool: Any) -> datetime | None:
    """Read the persisted auto-fetch last-run stamp, or ``None`` if unusable."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
                AUTO_PIPELINE_LAST_RUN_KEY,
            )
    except Exception:
        logger.exception("auto_pipeline: failed to read the last-run stamp")
        return None
    value = row["value"] if row is not None else None
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("auto_pipeline: last-run stamp %r is not a timestamp", value)
        return None
    # A hand-edited row may be naive; comparing it to an aware ``now`` would raise.
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _log_schedule(scheduler: AsyncIOScheduler) -> None:
    """Log when every registered job next fires, so the schedule is observable."""
    for job in scheduler.get_jobs():
        logger.info("scheduler job %s next fires at %s", job.id, job.next_run_time)


async def _schedule_auto_pipeline_catchup(
    scheduler: AsyncIOScheduler, app: Any, interval_hours: float, effective_interval: float
) -> None:
    """Pull the interval job forward when its last fire was missed.

    Jobs live in memory, so an interval fire due while the service was down is
    simply lost. A stale last-run stamp means exactly that happened: run once
    shortly after boot, leaving the service time to finish starting up.

    This MOVES the existing job rather than registering a second one. Two ids
    running the same pipeline would each get their own ``max_instances``, so a
    boot whose anchored fire landed in this window would run the pipeline twice
    over. The interval trigger re-anchors to whichever fire actually happened,
    so the spacing is preserved and only the phase shifts, until the next
    restart re-anchors it to the fixed epoch.
    """
    if interval_hours <= 0:
        return
    last_run = await _read_auto_pipeline_last_run(app.state.db_pool)
    now = datetime.now(UTC)
    if last_run is not None and now - last_run < timedelta(hours=effective_interval):
        return
    scheduler.modify_job("auto_pipeline", next_run_time=now + timedelta(minutes=2))
    logger.info("auto_pipeline catch-up scheduled (last successful run: %s)", last_run)


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

    if user_ids is None:
        user_ids = await _list_active_users(db_pool)
        if user_ids is None:
            logger.warning(
                "%s: could not read active users — skipping %s deferral",
                log_label,
                task_kind,
            )
            return 0
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


async def run_zotero_sync_wrapper(app: Any, user_id: int | None = None) -> None:
    """APScheduler entrypoint for Zotero library sync — defers via procrastinate."""
    if skip_for_maintenance("zotero sync"):
        return
    db_pool = app.state.db_pool
    try:
        ready_user_ids = await _list_zotero_polling_users(db_pool)
        if user_id is not None:
            if user_id not in ready_user_ids:
                logger.info("zotero: polling not ready for user %d; skipping", user_id)
                return
            ready_user_ids = [user_id]
        # A poll already running for a user re-imports the same items; the job
        # itself refuses the duplicate, this only avoids enqueueing it.
        free_user_ids = await _users_without_active_lock(
            db_pool, ready_user_ids, kind="zotero.sync_from_zotero"
        )
        if not free_user_ids:
            logger.info("zotero: all ready users have an in-flight sync — skipping")
            return
        await _defer_per_user(
            task_kind="zotero.sync_from_zotero",
            db_pool=db_pool,
            log_label="zotero",
            user_ids=free_user_ids,
        )
    except Exception:
        logger.exception("zotero: failed to defer sync job")


async def run_pulse_wrapper(app: Any) -> None:
    """APScheduler entrypoint — gated on ``pulse.enabled`` config.

    Skips users whose pulse advisory lock is currently held (i.e. a
    ``/pulse_now`` or earlier cron job is still running) to prevent stacking
    duplicate pipeline runs.
    """
    if skip_for_maintenance("pulse"):
        return
    db_pool = app.state.db_pool
    if not await _is_pulse_enabled(db_pool):
        logger.info("pulse: disabled via user_config, skipping nightly run")
        return
    try:
        active_users = await _list_active_users(db_pool)
        if active_users is None:
            logger.warning("pulse: could not read active users — skipping nightly run")
            return
        if not active_users:
            logger.info("pulse: no active users — skipping nightly run")
            return
        user_ids = await _users_without_active_lock(db_pool, active_users, kind="pulse.generate")
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
    if skip_for_maintenance("pulse classifier training"):
        return
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
    if skip_for_maintenance("weekly digest"):
        return
    db_pool = app.state.db_pool  # touch to surface AttributeError early in tests
    try:
        await _defer_per_user(
            task_kind="digest.weekly", db_pool=db_pool, log_label="digest", days=7
        )
    except Exception:
        logger.exception("digest: failed to defer weekly digest job")


async def purge_system_events_task(app: Any) -> None:
    """Tiered retention: 30 days for app events, 7 days for infra events."""
    if skip_for_maintenance("purge system events"):
        return
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


# Named so the contract case can execute the statement the service runs rather
# than a copy of it that could drift.
ORPHANED_JOB_PROGRESS_PURGE = (
    "DELETE FROM job_progress WHERE NOT EXISTS ("
    "SELECT 1 FROM procrastinate_jobs pj"
    " WHERE pj.args->>'job_id' = job_progress.jarvis_job_id)"
)


async def purge_job_history_task(app: Any) -> None:
    """Prune finished procrastinate jobs and the progress rows left behind by them.

    Retention policy is owned here rather than in the shared worker hook: that
    hook is also used by the other service, and ``remove_old_jobs`` defaults to
    every queue, so a deferral sited there would prune across both services'
    histories.
    """
    if skip_for_maintenance("purge job history"):
        return
    db_pool = app.state.db_pool
    try:
        procrastinate_app = app.state.procrastinate_app
        # Looked up by exact registry key rather than by importing the task
        # object: the module-level object is shared process-wide across both
        # services' apps, while this key is registered on ours.
        await procrastinate_app.tasks[_REMOVE_OLD_JOBS_TASK].defer_async(
            max_hours=_JOB_HISTORY_MAX_HOURS,
            remove_failed=True,
            remove_cancelled=True,
            remove_aborted=True,
        )
        # Ordering against the deferral above is irrelevant: a deferral is
        # fire-and-forget, and this deletes the rows orphaned by any PRIOR
        # prune cycle, so both tables stay bounded either way.
        result = await db_pool.execute(ORPHANED_JOB_PROGRESS_PURGE)
        logger.info("purge_job_history: prune deferred, orphaned progress rows %s", result)
    except Exception:
        logger.exception("purge_job_history: failed to prune job history")


async def reconcile_zotero_poll_job(
    *,
    scheduler: Any,
    app: Any,
    db_pool: Any,
    user_id: int,
) -> None:
    """Add, replace, or remove one user's Zotero poll job from DB truth."""
    if scheduler is None:
        return
    job_id = _zotero_poll_job_id(user_id)
    schedules = await _list_zotero_polling_schedules(db_pool, user_id=user_id)
    if not schedules:
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)
            logger.info("%s scheduler removed; Zotero polling no longer ready", job_id)
        return

    _uid, cron_expr = schedules[0]
    scheduler.add_job(
        run_zotero_sync_wrapper,
        trigger=CronTrigger.from_crontab(cron_expr),
        args=[app, user_id],
        id=job_id,
        name=f"Zotero library sync for user {user_id}",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    logger.info("%s scheduler reconciled (cron=%s)", job_id, cron_expr)


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
        if skip_for_maintenance("recommendation refresh"):
            return
        try:
            count = await refresh_recommendations(app)
            logger.info("Nightly recommendations: %d saved", count)
        except Exception:
            logger.exception("Nightly recommendation refresh failed")

    scheduler = AsyncIOScheduler()

    def _register_cron(func: Any, *, cron: str, job_id: str, name: str) -> None:
        """Register one whole-app cron job with the shared overlap and misfire defaults.

        A bad cron expression must not take the remaining schedules down with it,
        so registration failures are logged and skipped.
        """
        try:
            scheduler.add_job(
                func,
                trigger=CronTrigger.from_crontab(cron),
                args=[app],
                id=job_id,
                name=name,
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
            logger.info("%s scheduler registered (cron=%s)", job_id, cron)
        except Exception:
            logger.exception("Failed to register %s job", job_id)

    # Register auto_pipeline unconditionally — the job self-gates when interval_hours <= 0.
    # This allows live-enabling via the Settings UI without restarting the service.
    _effective_interval = max(interval_hours, 1.0) if interval_hours > 0 else 24
    scheduler.add_job(
        run_auto_pipeline,
        trigger=IntervalTrigger(
            hours=_effective_interval,  # type: ignore[arg-type]
            start_date=_INTERVAL_EPOCH,
        ),
        args=[app],
        id="auto_pipeline",
        name="Auto fetch->process pipeline",
        replace_existing=True,
        max_instances=1,  # prevent overlap if a run takes longer than the interval
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _run_recommendations,
        IntervalTrigger(hours=24, start_date=_INTERVAL_EPOCH),
        args=[app],
        id="recommendation_refresh",
        name="Nightly recommendation refresh",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # Pulse classifier training (cron-scheduled before the overnight deck; gated on pulse.enabled)
    _register_cron(
        run_pulse_classifier_training_wrapper,
        cron=_DEFAULT_PULSE_CLASSIFIER_CRON,
        job_id="pulse_classifier_training",
        name="Pulse classifier retraining",
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
            misfire_grace_time=3600,
        )
        logger.info("pulse_overnight scheduler registered (cron=%s)", cron_expr)
    except Exception:
        logger.exception("Failed to register pulse_overnight job")

    # Weekly digest regeneration (cron-scheduled; GET /api/digest/weekly remains synchronous)
    _register_cron(
        run_weekly_digest_wrapper,
        cron=_DEFAULT_WEEKLY_DIGEST_CRON,
        job_id="weekly_digest",
        name="Weekly digest regeneration",
    )

    # Zotero library sync (per-user cron-scheduled, gated on each user's config)
    try:
        zotero_schedules = await _list_zotero_polling_schedules(app.state.db_pool)
        for uid, zotero_cron in zotero_schedules:
            scheduler.add_job(
                run_zotero_sync_wrapper,
                trigger=CronTrigger.from_crontab(zotero_cron),
                args=[app, uid],
                id=_zotero_poll_job_id(uid),
                name=f"Zotero library sync for user {uid}",
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
        logger.info("zotero_library_sync scheduler registered (%d users)", len(zotero_schedules))
    except Exception:
        logger.exception("Failed to register zotero_library_sync jobs")

    # Tiered system_events purge (daily at 2 AM)
    _register_cron(
        purge_system_events_task,
        cron=_DEFAULT_SYSTEM_EVENTS_PURGE_CRON,
        job_id="purge_system_events",
        name="Tiered system_events purge",
    )

    # Daily prune of finished job rows and their orphaned progress rows.
    _register_cron(
        purge_job_history_task,
        cron=_DEFAULT_JOB_HISTORY_PURGE_CRON,
        job_id="purge_job_history",
        name="Job history prune",
    )

    # Daily hard-purge of soft-deleted users past grace period.
    from paper_ingestion.jobs.data_purge import register_data_purge  # noqa: PLC0415

    register_data_purge(scheduler, app)

    # BE-09: daily purge of expired magic_link_tokens rows.
    from paper_ingestion.jobs.purge_tokens import register_purge_tokens  # noqa: PLC0415

    register_purge_tokens(scheduler, app)

    # Daily purge of stale (long-expired / long-revoked) session rows.
    from paper_ingestion.jobs.purge_sessions import register_purge_sessions  # noqa: PLC0415

    register_purge_sessions(scheduler, app)

    await _schedule_auto_pipeline_catchup(scheduler, app, interval_hours, _effective_interval)

    scheduler.start()
    logger.info("auto_pipeline scheduler started (interval=%.2fh)", interval_hours)
    _log_schedule(scheduler)
    return scheduler
