"""System release-readiness endpoint: GET /api/system/readiness."""

from datetime import UTC, datetime
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from jarvis_common import require_admin_or_api_key
from jarvis_common.logging_config import correlation_id_var
from jarvis_common.owner import resolve_owner_identity
from jarvis_common.settings import CoreSettings, get_core_settings, get_secrets_settings
from pydantic import BaseModel

from paper_ingestion.deps import limiter
from paper_ingestion.ingestion.payload_schema import visibility_checkpoint_progress
from paper_ingestion.services.backup_archive import (
    _last_run_succeeded,
    _list_entries,
    _read_last_run,
)

router = APIRouter(prefix="/api/system", tags=["system"])

ReadinessStatus = Literal["green", "amber", "red"]

# Ordered worst-to-best so aggregate selection is a simple max-by-rank.
_STATUS_RANK: dict[str, int] = {"green": 0, "amber": 1, "red": 2}


class ReadinessCheck(BaseModel):
    """One release-readiness probe result."""

    name: str
    status: ReadinessStatus
    detail: str
    remediation: str = ""


class ReadinessResponse(BaseModel):
    """Aggregate readiness report. ``status`` is the worst of ``checks``."""

    status: ReadinessStatus
    checks: list[ReadinessCheck]


# Granular dev-flag attribute names on CoreSettings. Each must be False for a
# production deployment; True means a safety bypass is active → red.
_DEV_FLAG_NAMES: tuple[str, ...] = (
    "dev_auth_bypass",
    "dev_error_detail",
    "dev_cors_open",
    "dev_smtp_log_only",
    "dev_crypto_relaxed",
)

_DEV_REMEDIATION: dict[str, str] = {
    "dev_auth_bypass": (
        "Set DEV_AUTH_BYPASS=false and DEV_MODE=false before sharing this URL — "
        "anyone who can reach this page can sign in as any user."
    ),
    "dev_error_detail": (
        "Set DEV_ERROR_DETAIL=false in production — full error tracebacks leak "
        "internal file paths and logic to anyone who triggers an error."
    ),
    "dev_cors_open": (
        "Set DEV_CORS_OPEN=false and restrict CORS_ORIGINS to your domain before "
        "going live — otherwise any website can silently act on behalf of a user."
    ),
    "dev_smtp_log_only": (
        "Set DEV_SMTP_LOG_ONLY=false and configure SMTP credentials (SMTP_HOST, "
        "SMTP_USER, SMTP_PASS) for production — otherwise delivery is suppressed "
        "and administrators must provide manual sign-in links."
    ),
    "dev_crypto_relaxed": (
        "Set DEV_CRYPTO_RELAXED=false in production — login tokens use weaker "
        "security and stay valid longer if stolen."
    ),
}


def _development_flag_checks(core: CoreSettings) -> list[ReadinessCheck]:
    """Build readiness results for production safety switches.

    Parameters
    ----------
    core : CoreSettings
        Effective process settings.

    Returns
    -------
    list[ReadinessCheck]
        One result per safety switch, in the stable display order.
    """
    return [
        ReadinessCheck(
            name=flag_name,
            status="red" if getattr(core, flag_name) else "green",
            detail="enabled" if getattr(core, flag_name) else "disabled",
            remediation=_DEV_REMEDIATION[flag_name],
        )
        for flag_name in _DEV_FLAG_NAMES
    ]


def _runtime_database_checks(request: Request) -> list[ReadinessCheck]:
    """Return the schema role and pool diagnostics captured at startup."""
    migration_check = getattr(request.app.state, "migration_check", None)
    if migration_check is None:
        return [
            ReadinessCheck(
                name="database_schema",
                status="red",
                detail="runtime schema check unavailable",
                remediation="Run the one-shot migrator and restart paper_ingestion.",
            )
        ]

    pool = request.app.state.db_pool

    def pool_metric(name: str) -> int | None:
        getter = getattr(pool, name, None)
        value = getter() if callable(getter) else None
        return value if isinstance(value, int) else None

    pool_size = pool_metric("get_size")
    pool_idle = pool_metric("get_idle_size")
    pool_max = pool_metric("get_max_size")
    return [
        ReadinessCheck(
            name="database_schema",
            status="green" if migration_check.integrity == "ok" else "red",
            detail=(
                f"role={migration_check.current_user}; "
                f"packaged={migration_check.packaged_version}; "
                f"live={migration_check.live_version}; integrity={migration_check.integrity}"
            ),
            remediation="Run the one-shot migrator before starting writers."
            if migration_check.integrity != "ok"
            else "",
        ),
        ReadinessCheck(
            name="migration_check",
            status="green",
            detail=(
                "outcome=success; duration_ms="
                f"{getattr(request.app.state, 'migration_check_duration_ms', 'unknown')}"
            ),
        ),
        ReadinessCheck(
            name="database_pool",
            status="amber" if pool_size == pool_max and pool_idle == 0 else "green",
            detail=f"size={pool_size}; idle={pool_idle}; max={pool_max}",
            remediation="Reduce concurrent work or increase the configured pool maximum."
            if pool_size == pool_max and pool_idle == 0
            else "",
        ),
        ReadinessCheck(
            name="correlation",
            status="green",
            detail=str(correlation_id_var.get() or "unavailable"),
        ),
    ]


async def _job_queue_readiness(db_pool: asyncpg.Pool) -> ReadinessCheck:
    """Report durable queue age and failure counts without telemetry exporters."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE jobs.status = 'failed')::int AS failures,
                    COUNT(*) FILTER (WHERE jobs.status IN ('todo', 'doing'))::int AS active,
                    COALESCE(
                        MAX(GREATEST(0, EXTRACT(EPOCH FROM (
                            NOW() - COALESCE(jobs.scheduled_at, events.enqueued_at, NOW())
                        )))) FILTER (WHERE jobs.status IN ('todo', 'doing')),
                        0
                    )::int AS oldest_active_seconds
                FROM ops.procrastinate_jobs AS jobs
                LEFT JOIN (
                    SELECT job_id, MIN(at) AS enqueued_at
                    FROM ops.procrastinate_events
                    GROUP BY job_id
                ) AS events ON events.job_id = jobs.id
                """
            )
        failures = int(row["failures"] if row is not None else 0)
        active = int(row["active"] if row is not None else 0)
        oldest = int(row["oldest_active_seconds"] if row is not None else 0)
        degraded = failures > 0 or oldest > 300
        return ReadinessCheck(
            name="job_queue",
            status="amber" if degraded else "green",
            detail=f"active={active}; failures={failures}; oldest_active_seconds={oldest}",
            remediation=(
                "Inspect failed jobs and worker heartbeats; retry only after the cause is fixed."
                if degraded
                else ""
            ),
        )
    except Exception as exc:  # noqa: BLE001 — readiness reports bounded failure type
        return ReadinessCheck(
            name="job_queue",
            status="amber",
            detail=f"unavailable: {type(exc).__name__}",
            remediation="Check the operations schema grants and worker database connectivity.",
        )


async def _outbox_readiness(db_pool: asyncpg.Pool) -> ReadinessCheck:
    """Report durable lag, retry, and dead-letter facts for the Research outbox."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                    COUNT(*) FILTER (
                        WHERE delivered_at IS NULL AND dead_lettered_at IS NULL
                    )::int AS pending,
                    COUNT(*) FILTER (
                        WHERE attempts > 0 AND delivered_at IS NULL AND dead_lettered_at IS NULL
                    )::int AS retries,
                    COUNT(*) FILTER (WHERE dead_lettered_at IS NOT NULL)::int AS dead_letters,
                    COALESCE(MAX(EXTRACT(EPOCH FROM NOW() - created_at)) FILTER (
                        WHERE delivered_at IS NULL AND dead_lettered_at IS NULL
                    ), 0)::int AS lag_seconds
                FROM domain_events"""
            )
            if row is None:
                raise RuntimeError("outbox diagnostic returned no row")
            pending, retries, dead_letters, lag = (
                int(row["pending"]),
                int(row["retries"]),
                int(row["dead_letters"]),
                int(row["lag_seconds"]),
            )
    except Exception as exc:  # noqa: BLE001 — readiness reports bounded failure type
        return ReadinessCheck(
            name="outbox",
            status="amber",
            detail=f"unavailable: {type(exc).__name__}",
            remediation="Check the operations schema grants and database connectivity.",
        )
    return ReadinessCheck(
        name="outbox",
        status="amber" if retries or dead_letters or lag > 300 else "green",
        detail=(
            f"pending={pending}; retries={retries}; dead_letters={dead_letters}; lag_seconds={lag}"
        ),
        remediation=(
            "Inspect Learning command delivery and dead letters before cutover."
            if retries or dead_letters or lag > 300
            else ""
        ),
    )


def _backup_readiness() -> ReadinessCheck:
    """Report the latest durable backup result and age from the read-only mount."""
    entries = _list_entries()
    run = _read_last_run()
    succeeded = _last_run_succeeded(run)
    last_success = entries[0].modified_at if entries else None
    age_seconds: int | None = None
    if last_success is not None:
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=UTC)
        age_seconds = max(0, int((datetime.now(UTC) - last_success).total_seconds()))
    stale = age_seconds is None or age_seconds > 129_600
    status: ReadinessStatus = (
        "red" if succeeded is False else "amber" if succeeded is None or stale else "green"
    )
    result = "unknown" if succeeded is None else "success" if succeeded else "failure"
    return ReadinessCheck(
        name="backup",
        status=status,
        detail=(
            f"result={result}; age_seconds={age_seconds if age_seconds is not None else 'unknown'}"
        ),
        remediation=(
            "Inspect the backup sidecar result and take a fresh verified backup."
            if status != "green"
            else ""
        ),
    )


async def _cutover_readiness_checks(request: Request) -> list[ReadinessCheck]:
    """Collect profile-independent queue, outbox, and backup diagnostics."""
    return [
        await _job_queue_readiness(request.app.state.db_pool),
        await _outbox_readiness(request.app.state.db_pool),
        _backup_readiness(),
    ]


async def _audit_log_readiness(db_pool: asyncpg.Pool) -> ReadinessCheck:
    """Report whether the Platform audit log remains queryable from Research."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS n FROM platform.audit_log")
        count = row["n"] if row is not None else 0
        return ReadinessCheck(name="audit_log", status="green", detail=f"{count} rows")
    except Exception as exc:  # noqa: BLE001 — expose bounded type, never raw DB detail
        return ReadinessCheck(
            name="audit_log",
            status="amber",
            detail=type(exc).__name__,
            remediation="The audit log could not be queried. Check connectivity and grants.",
        )


async def _vector_visibility_readiness(db_pool: asyncpg.Pool) -> ReadinessCheck:
    """Report whether every vector uses the active visibility generation.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Pool containing the durable reconciliation checkpoint.

    Returns
    -------
    ReadinessCheck
        Green after reconciliation completes; otherwise amber with bounded
        progress or a connectivity-oriented remediation.

    Notes
    -----
    Authenticated vector search fails closed while this check is amber.
    """
    try:
        progress = await visibility_checkpoint_progress(db_pool)
        checkpoint_status = str(progress["status"])
        current = max(0, int(progress.get("last_chunk_id", 0)))
        total = max(0, int(progress.get("total_chunk_id", 0)))
        current = min(current, total) if total else 0
        return ReadinessCheck(
            name="vector_visibility_metadata",
            status="green" if checkpoint_status == "complete" else "amber",
            detail=(
                "complete"
                if checkpoint_status == "complete"
                else f"{checkpoint_status}: {current}/{total}"
            ),
            remediation=(
                "Keep paper_ingestion running until vector visibility metadata repair "
                "completes. Check Qdrant and PostgreSQL connectivity if progress stalls."
                if checkpoint_status != "complete"
                else ""
            ),
        )
    except Exception as exc:  # noqa: BLE001 — readiness must remain reachable
        return ReadinessCheck(
            name="vector_visibility_metadata",
            status="amber",
            detail=f"unavailable: {type(exc).__name__}",
            remediation=(
                "Check Qdrant and PostgreSQL connectivity, then restart "
                "paper_ingestion to resume metadata repair."
            ),
        )


@router.get(
    "/readiness",
    response_model=ReadinessResponse,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_readiness(request: Request) -> ReadinessResponse:
    """Return the authenticated deployment-readiness report.

    Parameters
    ----------
    request : Request
        FastAPI request carrying the database pool, effective scheme, and
        dependency-authenticated administrator or operations API-key identity.

    Returns
    -------
    ReadinessResponse
        Stable per-check results and the worst aggregate status, ordered
        ``red`` over ``amber`` over ``green``.

    Notes
    -----
    SMTP is optional because administrators can deliver one-time links
    manually. Vector visibility stays amber until the current generation is
    fully reconciled; authenticated vector search fails closed by under-fetching
    while that repair is incomplete or unavailable.
    """
    core = get_core_settings()
    secrets = get_secrets_settings()
    checks: list[ReadinessCheck] = []

    checks.extend(_development_flag_checks(core))
    checks.extend(_runtime_database_checks(request))
    checks.extend(await _cutover_readiness_checks(request))

    # Environment.
    env_value = core.environment
    checks.append(
        ReadinessCheck(
            name="environment",
            status="green" if env_value == "production" else "amber",
            detail=env_value,
            remediation=(
                "Set ENVIRONMENT=production before going live — some safeguards "
                "(rate-limits, security headers) only activate in production mode."
            ),
        )
    )

    # API key — presence + minimum length, never echoed.
    api_key = secrets.jarvis_api_key
    if api_key is None:
        checks.append(
            ReadinessCheck(
                name="api_key",
                status="red",
                detail="missing",
                remediation=(
                    "Generate a 32-byte key with: openssl rand -hex 32. "
                    "Then set JARVIS_API_KEY to that value."
                ),
            )
        )
    elif len(api_key.get_secret_value()) >= 32:
        checks.append(
            ReadinessCheck(
                name="api_key",
                status="green",
                detail="configured (>=32 chars)",
                remediation="",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="api_key",
                status="red",
                detail="too short",
                remediation=(
                    "Generate a new 32-byte key with: openssl rand -hex 32. "
                    "Then set JARVIS_API_KEY to that value."
                ),
            )
        )

    # Deployment owner — required for bounded API-key recovery and safe
    # ownership transfer, but never a service-liveness or SMTP prerequisite.
    try:
        async with request.app.state.db_pool.acquire() as conn:
            owner = await resolve_owner_identity(conn)
        if owner.is_valid:
            checks.append(
                ReadinessCheck(
                    name="owner_identity",
                    status="green",
                    detail=f"{owner.source}: valid",
                )
            )
        elif owner.source == "environment":
            checks.append(
                ReadinessCheck(
                    name="owner_identity",
                    status="amber",
                    detail=f"environment: {owner.state}",
                    remediation=(
                        "Correct or remove OWNER_USER_ID in the host .env, then restart "
                        "paper_ingestion. Environment-managed ownership cannot be repaired "
                        "from the browser."
                    ),
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    name="owner_identity",
                    status="amber",
                    detail=f"{owner.source}: {owner.state}",
                    remediation=(
                        "On the JARVIS host, run `jarvis-research owner status`, then "
                        "use `jarvis-research owner set <admin-email>` if it reports "
                        "that repair is required."
                    ),
                )
            )
    except Exception as exc:  # noqa: BLE001 — readiness must remain reachable
        checks.append(
            ReadinessCheck(
                name="owner_identity",
                status="amber",
                detail=f"unavailable: {type(exc).__name__}",
                remediation=(
                    "On the JARVIS host, run `jarvis-research owner status` and check "
                    "database connectivity before attempting repair."
                ),
            )
        )

    # Probe the effective relay (wizard-written user_config layered over env)
    # and its auth consistency. SMTP is optional because an administrator can
    # create a one-time link for private manual delivery.
    from jarvis_common.email import effective_smtp_status  # noqa: PLC0415

    smtp_deliverable, smtp_issues = await effective_smtp_status(request.app.state.db_pool)
    if smtp_deliverable and not smtp_issues:
        smtp_status = "green"
        smtp_detail = "configured"
    elif smtp_deliverable:
        # Deliverable envelope but a config issue (e.g. username without password).
        smtp_status = "amber"
        smtp_detail = "configured with warnings"
    else:
        smtp_status = "amber"
        smtp_detail = "not configured — administrators can create manual sign-in links"
    checks.append(
        ReadinessCheck(
            name="smtp",
            status=smtp_status,
            detail=smtp_detail,
            remediation=(
                "Configure SMTP for automatic email delivery, or use Admin > Users "
                "to create and privately share a manual invitation or recovery link. "
                "If SMTP_USER is set, SMTP_PASS must also be set."
            ),
        )
    )

    # HTTPS — inferred from the request / proxy header (no settings field).
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    scheme = forwarded_proto or request.url.scheme
    checks.append(
        ReadinessCheck(
            name="https",
            status="green" if scheme == "https" else "amber",
            detail=scheme,
            remediation=(
                "Ensure TLS is terminated at the edge — Caddy/nginx handles this "
                "automatically when pointed at a real domain."
            ),
        )
    )

    checks.append(await _audit_log_readiness(request.app.state.db_pool))
    checks.append(await _vector_visibility_readiness(request.app.state.db_pool))

    aggregate = max(checks, key=lambda c: _STATUS_RANK[c.status]).status
    return ReadinessResponse(status=aggregate, checks=checks)
