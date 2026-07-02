"""System release-readiness endpoint: GET /api/system/readiness."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from jarvis_common import require_admin_or_api_key
from jarvis_common.settings import get_core_settings, get_secrets_settings
from pydantic import BaseModel

from paper_ingestion.deps import limiter

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


@router.get(
    "/readiness",
    response_model=ReadinessResponse,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_readiness(request: Request) -> ReadinessResponse:
    """Pre-public-launch readiness report: dev flags, env, secrets, HTTPS, audit log."""
    core = get_core_settings()
    secrets = get_secrets_settings()
    checks: list[ReadinessCheck] = []

    # Granular dev flags — each must be off for production.
    # Per-check remediation strings (what to set + concrete risk).
    _dev_remediation: dict[str, str] = {
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
            "SMTP_USER, SMTP_PASS) for production — otherwise sign-in emails print to "
            "logs and users never receive them."
        ),
        "dev_crypto_relaxed": (
            "Set DEV_CRYPTO_RELAXED=false in production — login tokens use weaker "
            "security and stay valid longer if stolen."
        ),
    }
    for flag_name in _DEV_FLAG_NAMES:
        enabled = bool(getattr(core, flag_name))
        checks.append(
            ReadinessCheck(
                name=flag_name,
                status="red" if enabled else "green",
                detail="enabled" if enabled else "disabled",
                remediation=_dev_remediation.get(flag_name, ""),
            )
        )

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

    # SMTP — magic links fall back to stdout when unset. Probe the EFFECTIVE
    # relay (wizard-written user_config layered over env) AND its auth-consistency,
    # so a host+sender present with a username-but-no-password (which 535s at AUTH)
    # does NOT report green. Matches the Settings banner (effective_smtp_status).
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
        smtp_detail = "not configured — magic links go to stdout"
        # A multi-user production box has no other login path for non-owner
        # users, so a missing relay is a hard failure (red), not a warning.
        if core.environment.lower() == "production":
            async with request.app.state.db_pool.acquire() as conn:
                user_count = int(
                    await conn.fetchval("SELECT count(*) FROM users WHERE deleted_at IS NULL") or 0
                )
            if user_count > 1:
                smtp_status = "red"
    checks.append(
        ReadinessCheck(
            name="smtp",
            status=smtp_status,
            detail=smtp_detail,
            remediation=(
                "Configure SMTP_HOST, SMTP_USER, SMTP_PASS, and SMTP_FROM "
                "before inviting real users — otherwise sign-in emails print to logs "
                "or the relay rejects sign-in at AUTH."
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

    aggregate = max(checks, key=lambda c: _STATUS_RANK[c.status]).status
    return ReadinessResponse(status=aggregate, checks=checks)
