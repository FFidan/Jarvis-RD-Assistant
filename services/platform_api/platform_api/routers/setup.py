"""First-run setup wizard endpoints.

These endpoints are the bootstrap path for a fresh install. They allow an
unauthenticated caller to:

1. Check whether the install has been "configured" (≥ 1 admin user exists).
2. Probe the dependency surface (Postgres / Qdrant / Ollama / LiteLLM).
3. Persist SMTP settings + optionally fire a test email.
4. Create the FIRST admin user AND atomically create a session for the same
   browser (no magic-link round-trip — the operator is sitting in front of
   the wizard).
5. Persist optional cloud LLM provider keys (Fernet-encrypted at rest).

Access control
--------------
``require_unconfigured_or_admin`` is awaited INLINE in the endpoint body by
every endpoint except two:

* ``/status`` — the pre-auth boot poll ``FirstRunGate`` issues before any
  credential exists.
* first-admin creation — self-guarded, refusing with 409 once an admin exists.

The gate itself resolves as:

* When ``users`` is empty → no auth required (this IS the bootstrap).
* When ≥ 1 admin user exists → caller must have ``role='admin'`` per the
  session cookie.

No route declares it as a FastAPI dependency, so a new endpoint must call
``await require_unconfigured_or_admin(request)`` itself; naming it in a comment
or docstring gates nothing.

The app-level ``verify_api_key`` returns early for any path starting with
``/api/setup/``, so these endpoints answer without a session or API key. That
exemption exists because ``FirstRunGate`` polls ``/api/setup/status`` on every
boot with no credential in hand; without it the UI hangs on a 403.

Naming note
-----------
This router lives at ``/api/setup/*``. The pre-existing
``/api/system/setup-status`` endpoint (post-login bootstrap wizard) is a
*different* surface — it tracks topic / Pulse / Telegram setup AFTER the
admin is logged in. Both can coexist.
"""

import asyncio
import hmac
import logging
import os
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from jarvis_common.audit import log_audit_strict
from jarvis_common.config_flags import coerce_bool
from jarvis_common.crypto import encrypt_secret
from jarvis_common.email import (
    MAX_EMAIL_LENGTH,
    _effective_smtp,
    effective_smtp_status,
    probe_smtp_reachable,
    sanitize_header_value,
    smtp_tls_flags,
)
from jarvis_common.email import smtp_configured as _smtp_configured_probe
from jarvis_common.maintenance import OutboundEgressBlockedError, ensure_outbound_egress_allowed
from jarvis_common.net import _reject_non_public_host
from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY
from jarvis_common.pinned_transport import (
    PUBLIC_ONLY,
    connect_pinned_socket,
    policy_allowing_private_host,
)
from jarvis_common.session_middleware import mint_session
from jarvis_common.settings import get_core_settings, get_secrets_settings
from pydantic import BaseModel, EmailStr, Field, field_validator

from platform_api.config import get_platform_settings
from platform_api.config_metadata import ENCRYPTED_CONFIG_KEYS
from platform_api.deps import limiter
from platform_api.routers.auth import _hash_email, _require_local_or_https

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

SMTP_TEST_TIMEOUT_SECONDS = 10.0

# Encrypted user_config keys this router writes. Must be a subset of the
# allow-list maintained in routers/settings.py — these keys are intentionally
# duplicated here rather than imported because settings.py is not on the
# setup write scope (and an import would create a circular surface).
_SMTP_PLAINTEXT_KEYS = (
    "smtp.host",
    "smtp.port",
    "smtp.user",
    "smtp.from",
    "smtp.reply_to",
    "smtp.from_name",
)
_SMTP_ENCRYPTED_KEYS = ("smtp.pass",)
_CLOUD_LLM_KEY_MAP = {
    "openai": "llm.openai.api_key",
    "anthropic": "llm.anthropic.api_key",
    "gemini": "llm.google.api_key",
}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SetupStatusResponse(BaseModel):
    """Describe installer progress and effective deployment capabilities.

    Attributes
    ----------
    configured : bool
        Whether the required first administrator exists.
    setup_completed : bool, optional
        Whether the installer recorded completion.
    setup_mode : {"single", "multi"}, optional
        Effective tenancy mode.
    hw_tier_baseline : str or None, optional
        Hardware tier selected during setup.
    hw_tier_current : str or None, optional
        Hardware tier inferred from the current host.
    hw_tier_changed : bool, optional
        Whether the current tier differs from the setup baseline.
    gpu_vendor : str, optional
        Configured or detected GPU vendor.
    access_mode : str, optional
        Installer-selected public access mode.
    recommended_backend : str or None, optional
        Backend recommended for the current hardware.
    current_backend : str or None, optional
        Backend selected in configuration.
    observed_backend : str or None, optional
        Backend observed in recent request telemetry.
    observed_recent_share : float, optional
        Fraction of recent requests using the observed backend.
    smtp_configured : bool, optional
        Whether effective SMTP settings are complete.
    smtp_reachable : bool, optional
        Whether the configured relay passed its cached liveness probe.
    """

    configured: bool
    setup_completed: bool = False
    setup_mode: Literal["single", "multi"] = "single"
    hw_tier_baseline: str | None = None
    hw_tier_current: str | None = None
    hw_tier_changed: bool = False
    # GPU vendor: the setup-written JARVIS_GPU_VENDOR (host truth) when
    # present, else inferred in-container (nvidia | amd | intel | none).
    gpu_vendor: str = "none"
    # Access mode written by setup.sh
    # (localhost | lan | tailscale | tunnel | letsencrypt).
    # Lets the frontend explain in-product which sign-in capabilities work here.
    access_mode: str = "localhost"
    recommended_backend: str | None = None
    current_backend: str | None = None
    observed_backend: str | None = None
    observed_recent_share: float = 0.0
    # True iff SMTP is fully configured (DB or env). Consumed pre-auth by
    # LoginPage to default to the API-key tab when no mail relay is configured,
    # avoiding a lockout for non-owner users in multi-user deployments.
    smtp_configured: bool = False
    # True iff the configured relay currently accepts a connection (cached
    # liveness probe). ``smtp_configured`` is presence-only, so a relay can be
    # configured but unreachable; this lets LoginPage/health show "configured
    # but currently failing". False whenever SMTP is not configured.
    smtp_reachable: bool = False


class ServiceStatus(BaseModel):
    """Describe one dependency checked by the setup diagnostics.

    Attributes
    ----------
    name : str
        Stable service name.
    ok : bool
        Whether the dependency check passed.
    detail : str or None, optional
        Bounded diagnostic detail.
    """

    name: str
    ok: bool
    detail: str | None = None


class SystemCheckResponse(BaseModel):
    """Aggregate setup dependency checks.

    Attributes
    ----------
    services : list[ServiceStatus]
        Per-service diagnostic results.
    all_ok : bool
        Whether every required service check passed.
    """

    services: list[ServiceStatus]
    all_ok: bool


class SmtpBody(BaseModel):
    """Validate SMTP relay settings submitted by an administrator.

    Attributes
    ----------
    host : str
        Relay hostname.
    port : int
        Relay TCP port.
    user : str or None, optional
        Authentication username.
    password : str or None, optional
        Authentication password, accepted under the ``pass`` API alias.
    from_email : EmailStr
        Envelope sender address.
    reply_to : str or None, optional
        Reply-to address; an empty string clears the value.
    from_name : str or None, optional
        Display name; an empty string clears the value.
    test_send : bool, optional
        Whether to send a probe message after saving.
    test_recipient : EmailStr or None, optional
        Explicit recipient for the probe message.
    """

    host: Annotated[str, Field(min_length=1, max_length=255)]
    port: Annotated[int, Field(ge=1, le=65535)]
    user: Annotated[str | None, Field(default=None, max_length=255)] = None
    password: Annotated[str | None, Field(default=None, max_length=512, alias="pass")] = None
    from_email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LENGTH)]
    # Optional sender identity. None = keep existing, "" = clear. Not secrets.
    reply_to: Annotated[str | None, Field(default=None, max_length=MAX_EMAIL_LENGTH)] = None
    from_name: Annotated[str | None, Field(default=None, max_length=255)] = None
    test_send: bool = False
    test_recipient: Annotated[
        EmailStr | None,
        Field(default=None, max_length=MAX_EMAIL_LENGTH),
    ] = None

    model_config = {"populate_by_name": True}

    @field_validator("host", mode="before")
    @classmethod
    def _normalize_host(cls, v: str) -> str:
        value = v.strip() if isinstance(v, str) else v
        if not value:
            raise ValueError("host must not be blank")
        return value

    @field_validator("from_email", mode="before")
    @classmethod
    def _normalize_from_email(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("reply_to")
    @classmethod
    def _validate_reply_to(cls, v: str | None) -> str | None:
        """Allow None (keep) and "" (clear); otherwise require a valid email."""
        if v is None or v == "":
            return v
        v = v.strip()
        if not v:
            return ""
        if not re.match(r"^\S+@\S+\.\S+$", v):
            raise ValueError("reply_to must be a valid email address")
        return v

    @field_validator("from_name")
    @classmethod
    def _validate_from_name(cls, v: str | None) -> str | None:
        """Allow None (keep); whitespace-only → "" (clear); reject control chars."""
        if v is None:
            return None
        v = v.strip()
        if not v:
            return ""
        if any(c in v for c in ("\r", "\n", "\x00")) or not v.isprintable():
            raise ValueError("from_name must not contain control characters")
        return v


class SmtpResponse(BaseModel):
    """Report persistence and optional SMTP probe delivery.

    Attributes
    ----------
    saved : bool
        Whether the settings were persisted.
    test_sent : bool or None, optional
        Probe delivery result when a test was requested.
    test_error : str or None, optional
        Sanitized probe failure detail.
    """

    saved: bool
    test_sent: bool | None = None
    test_error: str | None = None


class SmtpConfigResponse(BaseModel):
    """Current SMTP relay config for the settings UI.

    The password is never returned — only ``has_password`` indicates whether a
    secret is stored. ``restart_required`` is ``False``: the magic-link sender
    now resolves SMTP from ``user_config`` (layered over env) at send time via
    ``jarvis_common.email._effective_smtp``, so wizard-saved changes take
    effect immediately — no service restart or hand-edited .env required.
    """

    host: str | None = None
    port: int | None = None
    user: str | None = None
    from_email: str | None = None
    reply_to: str | None = None
    from_name: str | None = None
    has_password: bool = False
    restart_required: bool = False
    # Effective-config (DB-over-env) health for the settings UI warning banner.
    deliverable: bool = False
    issues: list[str] = Field(default_factory=list)


class AdminBody(BaseModel):
    """Request creation of the first administrator.

    Attributes
    ----------
    email : EmailStr
        Email address for the bootstrap administrator.
    """

    email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LENGTH)]


class AdminResponse(BaseModel):
    """Describe the administrator created during bootstrap.

    Attributes
    ----------
    id : int
        Stable user identifier.
    email : str
        Administrator email address.
    role : str
        Persisted authorization role.
    """

    id: int
    email: str
    role: str


class CloudLlmKeysBody(BaseModel):
    """Accept optional credentials for supported cloud LLM providers.

    Attributes
    ----------
    openai : str or None, optional
        OpenAI API key.
    anthropic : str or None, optional
        Anthropic API key.
    gemini : str or None, optional
        Google Gemini API key.
    """

    openai: Annotated[str | None, Field(default=None, max_length=512)] = None
    anthropic: Annotated[str | None, Field(default=None, max_length=512)] = None
    gemini: Annotated[str | None, Field(default=None, max_length=512)] = None


class CloudLlmKeysResponse(BaseModel):
    """Report cloud-provider credentials saved by Platform.

    Attributes
    ----------
    saved_providers : list[str]
        Providers whose supplied credentials were persisted.
    applied_now : list[str], optional
        Compatibility field; Platform does not synchronously reconcile Research.
    restart_required : bool, optional
        Whether the save requires a service restart.
    """

    saved_providers: list[str]
    # Retained for API compatibility. Research owns LiteLLM reconciliation, so
    # Platform never reports a synchronous cross-domain application.
    applied_now: list[str] = Field(default_factory=list)
    restart_required: bool = False


# Telegram bot token shape — mirrors setup.sh's regex
# (``^[0-9]+:[A-Za-z0-9_-]{20,}$``): numeric bot id, colon, ≥20-char secret.
_TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]{20,}$")


class TelegramBotTokenBody(BaseModel):
    """Accept a Telegram bot token in Bot API format.

    Attributes
    ----------
    token : str
        Token containing a numeric bot identifier and secret suffix.
    """

    token: Annotated[str, Field(min_length=20, max_length=512)]

    @field_validator("token")
    @classmethod
    def _validate_token_shape(cls, v: str) -> str:
        if not _TELEGRAM_TOKEN_RE.fullmatch(v):
            raise ValueError(
                "Invalid Telegram bot token format "
                "(expected '<bot_id>:<secret>' with a ≥20-char secret)."
            )
        return v


class TelegramBotTokenResponse(BaseModel):
    """Report persistence and activation requirements for a bot token.

    Attributes
    ----------
    saved : bool
        Whether the token was persisted.
    restart_required : bool, optional
        Whether Telegram must restart to load the token.
    """

    saved: bool
    # The bot consumes its token at container start, so a save here needs the
    # telegram_bot container restarted (never a file edit) to take effect.
    restart_required: bool = True


class TelegramBotTokenStatusResponse(BaseModel):
    """Report whether a Telegram bot token is configured.

    Attributes
    ----------
    has_token : bool
        Whether Platform can resolve a non-empty token.
    """

    has_token: bool


class SetupModeBody(BaseModel):
    """Request a supported tenancy mode.

    Attributes
    ----------
    mode : {"single", "multi"}
        Replacement setup mode.
    """

    mode: Literal["single", "multi"]


class SetupModeResponse(BaseModel):
    """Report the effective tenancy mode after persistence.

    Attributes
    ----------
    mode : {"single", "multi"}
        Persisted setup mode.
    restart_required : bool, optional
        Whether services must restart for the setting to apply.
    """

    mode: Literal["single", "multi"]
    # get_status reads the saved mode from user_config on every poll and
    # get_core_settings() is uncached, so a /mode write takes effect on the
    # next status read — no restart needed.
    restart_required: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _admin_count(pool: Any) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND deleted_at IS NULL"
            )
        )


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _require_setup_token(request: Request) -> None:
    """Gate a bootstrap WRITE on the setup token, when one is configured.

    The token is an additional bootstrap factor (closes the unauthenticated
    first-admin-takeover window). It is enforced only for unsafe methods; the
    read-only setup probes stay open. When no token is configured the gate is a
    no-op in non-production (backward-compat for dev/legacy installs) but fails
    closed in production, mirroring the boot gate in ``validate_runtime_config``.
    """
    if request.method in _SAFE_METHODS:
        return
    expected = get_secrets_settings().jarvis_setup_token
    if expected is None:
        if get_core_settings().environment.lower() == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Setup is locked: JARVIS_SETUP_TOKEN is not configured. "
                    "Run scripts/init-secrets.sh (or set the secret) and retry."
                ),
            )
        logger.warning(
            "First-admin setup is unprotected: a state-changing setup request was "
            "accepted with no JARVIS_SETUP_TOKEN configured and no admin yet. Set "
            "JARVIS_SETUP_TOKEN (or run init-secrets) before exposing this instance."
        )
        return
    provided = request.headers.get("X-Setup-Token", "")
    if not hmac.compare_digest(provided.encode(), expected.get_secret_value().encode()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing setup token",
        )


async def require_unconfigured_or_admin(request: Request) -> None:
    """Allow the call IFF no admin exists, OR the caller is an admin.

    This is the bootstrap-vs-locked-down switch. Once an admin user is in the
    DB the wizard surface flips into admin-only; before then it is wide open
    so the operator can complete first-run setup.
    """
    _require_local_or_https(request)
    pool = request.app.state.db_pool
    admins = await _admin_count(pool)
    if admins == 0:
        _require_setup_token(request)
        return  # bootstrap mode
    role = getattr(request.state, "user_role", None)
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/status",
    response_model=SetupStatusResponse,
    dependencies=[],
)
@limiter.limit("30/minute")
async def get_status(request: Request) -> SetupStatusResponse:
    """Return ``{configured: true}`` iff at least one admin user exists.

    Always reachable — the SetupGate on the frontend polls this on every
    boot, so it must never 401/403/500 on a fresh DB.
    """
    from jarvis_common.hw_detect import detect_tier, detect_vendor  # noqa: PLC0415
    from jarvis_common.litellm_observer import observed_share  # noqa: PLC0415

    pool = request.app.state.db_pool
    env_mode = get_core_settings().jarvis_setup_mode

    baseline = os.getenv("JARVIS_HW_TIER") or None
    current = detect_tier()
    vendor = detect_vendor()
    access_mode = os.getenv("JARVIS_ACCESS_MODE") or "localhost"
    # Effective backend = explicit override, else the runtime default the LLM
    # router actually uses (rag.py: os.getenv("JARVIS_LLM_BACKEND", "ollama")).
    # recommended_backend (the tier suggestion) is reported separately — don't conflate.
    backend = os.getenv("JARVIS_LLM_BACKEND") or "ollama"
    served, share = observed_share("smart")
    recommended = "ollama"
    changed = bool(baseline and baseline != current)

    try:
        admins = await _admin_count(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
                "setup.completed",
            )
            mode_row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
                "setup.mode",
            )
    except Exception as exc:
        # Fail-closed: a DB failure must NOT report configured=False, because
        # that would let the setup wizard re-open and a second admin could be
        # created when one already exists.
        logger.exception("setup status: admin count query failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Setup status check failed",
        ) from exc
    configured = admins > 0
    setup_completed = coerce_bool(row["value"] if row else None, default=False)

    # Report the SAVED mode (user_config, layered over env) so the wizard
    # reflects a /mode write immediately — this is the only runtime reader of
    # the mode, so the write is live on the next poll with no restart.
    saved_mode = mode_row["value"] if mode_row else None
    mode = saved_mode if saved_mode in ("single", "multi") else env_mode

    # smtp_configured is computed OUTSIDE the fail-closed try above so a DB
    # hiccup here never converts a successful status response into a 503.
    # _smtp_configured_probe has its own DB-failure fallback (returns env value).
    smtp_ok = await _smtp_configured_probe(pool)
    # Reachability is a cached, short-timeout, non-raising liveness probe; it
    # returns (False, None) without connecting when SMTP is not deliverable, so
    # a fresh/unconfigured deployment incurs no network call here.
    smtp_reachable, _ = await probe_smtp_reachable(pool)

    return SetupStatusResponse(
        configured=configured,
        setup_completed=setup_completed,
        setup_mode=mode,
        hw_tier_baseline=baseline,
        hw_tier_current=current,
        hw_tier_changed=changed,
        gpu_vendor=vendor,
        access_mode=access_mode,
        recommended_backend=recommended,
        current_backend=backend,
        observed_backend=served,
        observed_recent_share=share,
        smtp_configured=smtp_ok,
        smtp_reachable=smtp_reachable,
    )


@router.post(
    "/system-check",
    response_model=SystemCheckResponse,
    dependencies=[],
)
@limiter.limit("10/minute")
async def system_check(request: Request) -> SystemCheckResponse:
    """Run a synchronous reachability probe against every required dependency.

    Mirrors the lifespan-style health checks in ``main._run_health_checks``
    but reports per-service detail strings so the wizard can render
    actionable error states.

    INFO-01: exception detail is redacted to ``"unreachable"`` for the
    unauthenticated bootstrap path (admin_count==0) to avoid leaking internal
    host/port strings.  Authenticated admins receive the full detail.
    """
    await require_unconfigured_or_admin(request)

    pool = request.app.state.db_pool
    http = request.app.state.http_client
    settings = get_platform_settings()

    # Determine whether the caller is an authenticated admin; if not, redact
    # exception detail so internal host/port strings don't leak to unauthenticated
    # operators in bootstrap mode (INFO-01).
    admin_count = await _admin_count(pool)
    is_authenticated_admin = (
        admin_count > 0 and getattr(request.state, "user_role", None) == "admin"
    )

    def _detail(exc: Exception) -> str:
        if is_authenticated_admin:
            return str(exc)[:200]
        logger.debug("system_check dependency unreachable: %s", exc, exc_info=True)
        return "unreachable"

    services: list[ServiceStatus] = []

    # PostgreSQL
    try:
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        services.append(ServiceStatus(name="postgres", ok=True))
    except Exception as exc:  # noqa: BLE001
        services.append(ServiceStatus(name="postgres", ok=False, detail=_detail(exc)))

    # Qdrant
    try:
        response = await asyncio.wait_for(
            http.get(f"{settings.qdrant_url}/healthz"),
            timeout=5.0,
        )
        services.append(
            ServiceStatus(
                name="qdrant",
                ok=response.status_code == 200,
                detail=None if response.status_code == 200 else f"HTTP {response.status_code}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        services.append(ServiceStatus(name="qdrant", ok=False, detail=_detail(exc)))

    # Ollama
    try:
        resp = await asyncio.wait_for(
            http.get(f"{settings.ollama_base_url}/api/tags"),
            timeout=5.0,
        )
        services.append(
            ServiceStatus(
                name="ollama",
                ok=resp.status_code == 200,
                detail=None if resp.status_code == 200 else f"HTTP {resp.status_code}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        services.append(ServiceStatus(name="ollama", ok=False, detail=_detail(exc)))

    # LiteLLM
    try:
        resp = await asyncio.wait_for(
            http.get(f"{settings.litellm_base_url}/health/readiness"),
            timeout=5.0,
        )
        services.append(
            ServiceStatus(
                name="litellm",
                ok=resp.status_code == 200,
                detail=None if resp.status_code == 200 else f"HTTP {resp.status_code}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        services.append(ServiceStatus(name="litellm", ok=False, detail=_detail(exc)))

    return SystemCheckResponse(services=services, all_ok=all(s.ok for s in services))


async def _persist_config(pool: Any, key: str, value: Any) -> None:
    """Insert/update a single user_config row, encrypting iff *key* is a canonical secret."""
    encrypted = key in ENCRYPTED_CONFIG_KEYS
    if encrypted:
        ciphertext = encrypt_secret(str(value)).encode("ascii")
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_config (user_id, key, value, encrypted_value)
                VALUES (NULL, $1, NULL, $2)
                ON CONFLICT (user_id, key) DO UPDATE
                    SET value = NULL, encrypted_value = $2, updated_at = NOW()
                """,
                key,
                ciphertext,
            )
    else:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_config (user_id, key, value)
                VALUES (NULL, $1, $2::jsonb)
                ON CONFLICT (user_id, key) DO UPDATE
                    SET value = $2::jsonb, updated_at = NOW()
                """,
                key,
                value,  # asyncpg JSONB codec handles encoding—no json.dumps
            )


async def _read_smtp_config(pool: Any) -> SmtpConfigResponse:
    """Read the persisted SMTP config (system-wide rows) with the password masked.

    The password row (``smtp.pass``) is Fernet-encrypted at rest; we only
    report whether it exists via ``has_password`` and never decrypt it here.
    """
    keys = (*_SMTP_PLAINTEXT_KEYS, *_SMTP_ENCRYPTED_KEYS)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value, encrypted_value FROM user_config "
            "WHERE key = ANY($1::text[]) AND user_id IS NULL",
            list(keys),
        )
    by_key = {r["key"]: r for r in rows}

    def _plain(key: str) -> str | None:
        row = by_key.get(key)
        if row is None:
            return None
        # asyncpg JSONB codec auto-decodes — value is already a Python scalar.
        value = row["value"]
        return None if value is None else str(value)

    port_raw = _plain("smtp.port")
    port: int | None = None
    if port_raw is not None:
        try:
            port = int(port_raw)
        except ValueError:
            port = None

    pass_row = by_key.get("smtp.pass")
    has_password = pass_row is not None and pass_row["encrypted_value"] is not None

    # Deliverability verdict reflects the EFFECTIVE relay (DB layered over env),
    # so an env-only deployment is not falsely reported as misconfigured.
    deliverable, issues = await effective_smtp_status(pool)
    return SmtpConfigResponse(
        host=_plain("smtp.host"),
        port=port,
        user=_plain("smtp.user"),
        from_email=_plain("smtp.from"),
        reply_to=_plain("smtp.reply_to"),
        from_name=_plain("smtp.from_name"),
        has_password=has_password,
        deliverable=deliverable,
        issues=issues,
    )


async def _send_test_email(body: SmtpBody, recipient: str, password: str | None) -> str | None:
    """Send a best-effort SMTP probe without leaking or using quarantined credentials.

    ``password`` is resolved by the caller: ``body.password`` when the operator
    re-typed it, otherwise the stored (effective) password so the test button
    works against an already-saved relay without re-entering the secret.

    Parameters
    ----------
    body : SmtpBody
        Validated relay settings supplied by the setup route.
    recipient : str
        Operator-approved address that receives the probe message.
    password : str or None
        Effective relay password, if configured.

    Returns
    -------
    str or None
        ``None`` when the relay accepts the message; otherwise a sanitized
        validation, quarantine, dependency, or connection error.
    """
    try:
        ensure_outbound_egress_allowed("setup SMTP test delivery")
    except OutboundEgressBlockedError:
        return "SMTP delivery is disabled until restored credentials are reviewed."

    try:
        import aiosmtplib  # noqa: PLC0415
    except ImportError:
        return "aiosmtplib not installed in this image"

    message = EmailMessage()
    from_name = sanitize_header_value(body.from_name)
    try:
        message["From"] = formataddr((from_name, body.from_email)) if from_name else body.from_email
    except (TypeError, ValueError):
        message["From"] = body.from_email
    message["To"] = recipient
    message["Subject"] = "JARVIS SMTP test"
    reply_to = sanitize_header_value(body.reply_to)
    if reply_to:
        try:
            message["Reply-To"] = reply_to
        except (TypeError, ValueError):
            pass
    message.set_content(
        "This is a test email from the JARVIS first-run setup wizard.\n"
        "If you received this, your SMTP relay is working.\n"
    )

    use_tls, start_tls = smtp_tls_flags(body.port)
    try:
        ensure_outbound_egress_allowed("setup SMTP test delivery")
        policy = (
            policy_allowing_private_host(body.host)
            if get_core_settings().allow_private_smtp_host
            else PUBLIC_ONLY
        )
        sock = await connect_pinned_socket(
            body.host,
            body.port,
            policy=policy,
            timeout=SMTP_TEST_TIMEOUT_SECONDS,
        )
        await aiosmtplib.send(
            message,
            hostname=body.host,
            port=None,
            sock=sock,
            username=body.user or None,
            password=password or None,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=SMTP_TEST_TIMEOUT_SECONDS,
        )
    except OutboundEgressBlockedError:
        return "SMTP delivery is disabled until restored credentials are reviewed."
    except Exception as exc:  # noqa: BLE001
        logger.warning("setup smtp test_send failed: %s", exc, exc_info=True)
        return "SMTP connection failed — check host, port, and credentials."
    return None


@router.get(
    "/smtp",
    response_model=SmtpConfigResponse,
    dependencies=[],
)
@limiter.limit("30/minute")
async def get_smtp_config(request: Request) -> SmtpConfigResponse:
    """Return the current SMTP config with the password masked.

    Same admin gate as the POST: open during bootstrap (no admin yet), then
    admin-only. Never returns the stored password — only ``has_password``.
    """
    await require_unconfigured_or_admin(request)
    pool = request.app.state.db_pool
    return await _read_smtp_config(pool)


@router.post(
    "/smtp",
    response_model=SmtpResponse,
    dependencies=[],
)
@limiter.limit("10/minute")
async def configure_smtp(body: SmtpBody, request: Request) -> SmtpResponse:
    """Persist SMTP config and optionally fire a test email."""
    await require_unconfigured_or_admin(request)

    if not get_core_settings().allow_private_smtp_host:
        try:
            await _reject_non_public_host(body.host)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="SMTP host resolves to a non-public address. "
                "Set ALLOW_PRIVATE_SMTP_HOST=true for an internal relay.",
            ) from exc

    pool = request.app.state.db_pool
    await _persist_config(pool, "smtp.host", body.host)
    await _persist_config(pool, "smtp.port", body.port)
    if body.user is not None:
        await _persist_config(pool, "smtp.user", body.user)
    await _persist_config(pool, "smtp.from", body.from_email)
    if body.password:
        await _persist_config(pool, "smtp.pass", body.password)
    # Optional sender identity: None = keep, "" = clear (stored empty, which the
    # sender treats as absent). Validated + not secrets.
    if body.reply_to is not None:
        await _persist_config(pool, "smtp.reply_to", body.reply_to)
    if body.from_name is not None:
        await _persist_config(pool, "smtp.from_name", body.from_name)

    test_sent: bool | None = None
    test_error: str | None = None
    if body.test_send:
        # During first-run bootstrap (no admin yet) this endpoint is
        # unauthenticated; force the test recipient to the sender so it cannot be
        # abused to mail arbitrary third parties. Once an admin exists the
        # endpoint is admin-gated and an arbitrary recipient is allowed.
        if await _admin_count(pool) == 0:
            recipient = body.from_email
        else:
            recipient = body.test_recipient or body.from_email
        # When the operator re-typed the password use it; otherwise fall back to
        # the stored (effective) password so the test button works against an
        # already-saved relay without forcing a secret re-entry.
        if body.password:
            test_password = body.password
        else:
            test_password = (await _effective_smtp(pool)).password
        err = await _send_test_email(body, recipient, test_password)
        test_sent = err is None
        test_error = err

    return SmtpResponse(saved=True, test_sent=test_sent, test_error=test_error)


@router.post(
    "/admin",
    response_model=AdminResponse,
    dependencies=[],
)
@limiter.limit("3/minute")
async def create_first_admin(
    body: AdminBody, request: Request, response: Response
) -> AdminResponse:
    """Create the very first admin user AND open a session in one atomic step.

    Behaviour:

    * Refuses (409) if any admin already exists — that case must go through
      the standard ``/api/admin/users`` invite flow.
    * Inserts ``users`` row with ``role='admin'``.
    * Creates a 30-day ``sessions`` row.
    * Sets the ``jarvis_session`` cookie on the response.

    No magic-link round-trip: the wizard operator is physically at the
    browser; forcing email round-trip here creates a usability cliff in dev
    mode where SMTP is not yet configured (chicken-and-egg).
    """
    # Reject an untrusted/plaintext transport before opening the transaction or
    # touching the setup token. Host and X-Forwarded-* cannot satisfy this gate.
    _require_local_or_https(request)
    pool = request.app.state.db_pool
    email_norm = body.email.lower().strip()
    now = datetime.now(UTC)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serialise concurrent first-admin creation attempts at the DB level.
            # pg_advisory_xact_lock blocks until the previous transaction commits
            # or rolls back, so exactly one caller wins the admin-count=0 check.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('create_first_admin'))")
            admin_count = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND deleted_at IS NULL"
                )
            )
            if admin_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An admin user already exists; use the invite flow instead.",
                )

            # Bootstrap confirmed (admin_count == 0): this is the genuine
            # first-admin creation, so gate it on the setup token. An
            # existing-admin probe still gets the informative 409 above.
            _require_setup_token(request)

            # Pre-existing soft-deleted user with the same email is a hard error;
            # operator should reach out for ops support rather than silently undelete.
            existing = await conn.fetchrow(
                "SELECT id, deleted_at FROM users WHERE email = $1",
                email_norm,
            )
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A user with that email already exists.",
                )

            user_row = await conn.fetchrow(
                """
                INSERT INTO users (email, role, last_login_at)
                VALUES ($1, 'admin', NOW())
                RETURNING id, email, role
                """,
                email_norm,
            )
            user_id = int(user_row["id"])

            # Record the first admin as the canonical owner so API-key→session
            # login resolves an owner on a later multi-user box even when the
            # OWNER_USER_ID env is unset. Same transaction as the user INSERT, so
            # it commits/rolls back atomically. An env OWNER_USER_ID still wins.
            await conn.execute(
                "INSERT INTO user_config (user_id, key, value) VALUES (NULL, $1, $2::jsonb) "
                "ON CONFLICT (user_id, key) DO UPDATE SET value = $2::jsonb, updated_at = NOW()",
                OWNER_USER_ID_CONFIG_KEY,
                user_id,
            )

            await log_audit_strict(
                conn,
                action="admin.owner.bootstrap",
                resource=OWNER_USER_ID_CONFIG_KEY,
                user_id=str(user_id),
                metadata={"source": "first_admin"},
            )

            await mint_session(conn, response, user_id, now=now)

    logger.info("setup: first admin created id=%s email_hash=%s", user_id, _hash_email(email_norm))
    return AdminResponse(id=user_id, email=user_row["email"], role=user_row["role"])


@router.post(
    "/cloud-llm-keys",
    response_model=CloudLlmKeysResponse,
    dependencies=[],
)
@limiter.limit("10/minute")
async def configure_cloud_llm_keys(
    body: CloudLlmKeysBody, request: Request
) -> CloudLlmKeysResponse:
    """Persist optional cloud-LLM provider API keys.

    Research owns LiteLLM delivery and observes these Platform-owned encrypted
    settings through its persistent reconciler. The request therefore returns
    after the durable write instead of importing or invoking Research code.

    Parameters
    ----------
    body : CloudLlmKeysBody
        Optional provider keys supplied by the operator.
    request : Request
        Request carrying the database pool and setup identity.

    Returns
    -------
    CloudLlmKeysResponse
        Saved provider names. ``applied_now`` remains empty because delivery is
        asynchronous and ``restart_required`` remains false.
    """
    await require_unconfigured_or_admin(request)

    pool = request.app.state.db_pool
    saved: list[str] = []
    for provider, value in (
        ("openai", body.openai),
        ("anthropic", body.anthropic),
        ("gemini", body.gemini),
    ):
        if value is None or not value.strip():
            continue
        await _persist_config(pool, _CLOUD_LLM_KEY_MAP[provider], value.strip())
        saved.append(provider)

    return CloudLlmKeysResponse(
        saved_providers=saved,
        applied_now=[],
        restart_required=False,
    )


@router.post(
    "/telegram-bot-token",
    response_model=TelegramBotTokenResponse,
    dependencies=[],
)
@limiter.limit("10/minute")
async def configure_telegram_bot_token(
    body: TelegramBotTokenBody, request: Request
) -> TelegramBotTokenResponse:
    """Persist the Telegram bot token (Fernet-encrypted).

    The token is consumed by the telegram_bot container at start, so this is
    durable in ``user_config`` (the source of truth) but needs a telegram_bot
    container restart to take effect — never a file edit.
    """
    await require_unconfigured_or_admin(request)
    pool = request.app.state.db_pool
    await _persist_config(pool, "telegram.bot_token", body.token)
    return TelegramBotTokenResponse(saved=True, restart_required=True)


@router.get(
    "/telegram-bot-token",
    response_model=TelegramBotTokenStatusResponse,
    dependencies=[],
)
@limiter.limit("30/minute")
async def get_telegram_bot_token_status(
    request: Request,
) -> TelegramBotTokenStatusResponse:
    """Report whether a Telegram bot token is stored. Never echoes the token."""
    await require_unconfigured_or_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config "
            "WHERE key = 'telegram.bot_token' AND user_id IS NULL",
        )
    has_token = row is not None and (row["encrypted_value"] is not None or row["value"] is not None)
    return TelegramBotTokenStatusResponse(has_token=has_token)


@router.post(
    "/mode",
    response_model=SetupModeResponse,
    dependencies=[],
)
@limiter.limit("10/minute")
async def configure_setup_mode(body: SetupModeBody, request: Request) -> SetupModeResponse:
    """Persist the single↔multi-user mode.

    Durable in ``user_config``. ``get_status`` is the only runtime reader and
    prefers this saved value over the env default, so the change is live on the
    next status poll — no restart required.
    """
    await require_unconfigured_or_admin(request)
    pool = request.app.state.db_pool
    await _persist_config(pool, "setup.mode", body.mode)
    return SetupModeResponse(mode=body.mode, restart_required=False)


__all__ = ["router", "require_unconfigured_or_admin"]
