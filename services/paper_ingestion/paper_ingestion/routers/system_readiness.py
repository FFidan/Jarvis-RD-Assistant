"""System release-readiness endpoint: GET /api/system/readiness."""

from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from jarvis_common import require_admin_or_api_key
from jarvis_common.owner import resolve_owner_identity
from jarvis_common.settings import CoreSettings, get_core_settings, get_secrets_settings
from pydantic import BaseModel

from paper_ingestion.deps import limiter
from paper_ingestion.ingestion.payload_schema import visibility_checkpoint_progress

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
            detail=f"{checkpoint_status}: {current}/{total}",
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

    # Audit log — count rows; informational only.
    try:
        async with request.app.state.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS n FROM audit_log")
        n = row["n"] if row is not None else 0
        checks.append(
            ReadinessCheck(
                name="audit_log",
                status="green",
                detail=f"{n} rows",
                remediation="",
            )
        )
    except Exception as exc:  # noqa: BLE001 — surface class name, not raw message
        checks.append(
            ReadinessCheck(
                name="audit_log",
                status="amber",
                detail=type(exc).__name__,
                remediation="The audit_log table could not be queried. Check connectivity.",
            )
        )

    checks.append(await _vector_visibility_readiness(request.app.state.db_pool))

    aggregate = max(checks, key=lambda c: _STATUS_RANK[c.status]).status
    return ReadinessResponse(status=aggregate, checks=checks)
