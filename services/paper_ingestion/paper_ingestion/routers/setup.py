"""First-run setup wizard endpoints (Phase 2 WS-2F).

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
A new ``require_unconfigured_or_admin`` dependency gates every endpoint:

* When ``users`` is empty → no auth required (this IS the bootstrap).
* When ≥ 1 admin user exists → caller must have ``role='admin'`` per the
  session cookie.

The router is exported with the ``auth_exempt`` marker and registered with
``dependencies=[]`` in ``main.py`` so it bypasses the global
``verify_api_key`` — same exemption shape as ``routers/auth.py`` /
``routers/admin.py`` (WS-2A / WS-2B).

Naming note
-----------
This router lives at ``/api/setup/*``. The pre-existing
``/api/system/setup-status`` endpoint (post-login bootstrap wizard) is a
*different* surface — it tracks topic / Pulse / Telegram setup AFTER the
admin is logged in. Both can coexist.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from jarvis_common.crypto import encrypt_secret
from jarvis_common.session_middleware import SESSION_COOKIE_NAME
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

# Marker — exempted from global verify_api_key at include time (see main.py).
router.auth_exempt = True  # type: ignore[attr-defined]

SESSION_TTL = timedelta(days=30)
MAX_EMAIL_LEN = 320  # RFC 5321
SMTP_TEST_TIMEOUT_SECONDS = 10.0

# Encrypted user_config keys this router writes. Must be a subset of the
# allow-list maintained in routers/settings.py — these keys are intentionally
# duplicated here rather than imported because settings.py is not on the
# WS-2F write scope (and an import would create a circular surface).
_SMTP_PLAINTEXT_KEYS = ("smtp.host", "smtp.port", "smtp.user", "smtp.from")
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
    configured: bool


class ServiceStatus(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class SystemCheckResponse(BaseModel):
    services: list[ServiceStatus]
    all_ok: bool


class SmtpBody(BaseModel):
    host: Annotated[str, Field(min_length=1, max_length=255)]
    port: Annotated[int, Field(ge=1, le=65535)]
    user: Annotated[str | None, Field(default=None, max_length=255)] = None
    password: Annotated[str | None, Field(default=None, max_length=512, alias="pass")] = None
    from_email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LEN)]
    test_send: bool = False
    test_recipient: Annotated[EmailStr | None, Field(default=None, max_length=MAX_EMAIL_LEN)] = None

    model_config = {"populate_by_name": True}


class SmtpResponse(BaseModel):
    saved: bool
    test_sent: bool | None = None
    test_error: str | None = None


class AdminBody(BaseModel):
    email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LEN)]


class AdminResponse(BaseModel):
    id: int
    email: str
    role: str


class CloudLlmKeysBody(BaseModel):
    openai: Annotated[str | None, Field(default=None, max_length=512)] = None
    anthropic: Annotated[str | None, Field(default=None, max_length=512)] = None
    gemini: Annotated[str | None, Field(default=None, max_length=512)] = None


class CloudLlmKeysResponse(BaseModel):
    saved_providers: list[str]


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


def _cookie_secure() -> bool:
    """Match Secure flag to runtime mode (mirrors auth.py)."""
    return os.environ.get("DEV_MODE", "false").lower() != "true"


async def require_unconfigured_or_admin(request: Request) -> None:
    """Allow the call IFF no admin exists, OR the caller is an admin.

    This is the bootstrap-vs-locked-down switch. Once an admin user is in the
    DB the wizard surface flips into admin-only; before then it is wide open
    so the operator can complete first-run setup.
    """
    pool = request.app.state.db_pool
    admins = await _admin_count(pool)
    if admins == 0:
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
async def get_status(request: Request) -> SetupStatusResponse:
    """Return ``{configured: true}`` iff at least one admin user exists.

    Always reachable — the SetupGate on the frontend polls this on every
    boot, so it must never 401/403/500 on a fresh DB.
    """
    pool = request.app.state.db_pool
    try:
        admins = await _admin_count(pool)
    except Exception:
        logger.exception("setup status: admin count query failed")
        # Fail-open: report unconfigured so the wizard can run / the operator
        # can recover. The DB error itself will surface in /api/setup/system-check.
        return SetupStatusResponse(configured=False)
    return SetupStatusResponse(configured=admins > 0)


@router.post(
    "/system-check",
    response_model=SystemCheckResponse,
    dependencies=[],
)
async def system_check(request: Request) -> SystemCheckResponse:
    """Run a synchronous reachability probe against every required dependency.

    Mirrors the lifespan-style health checks in ``main._run_health_checks``
    but reports per-service detail strings so the wizard can render
    actionable error states.
    """
    import asyncio  # noqa: PLC0415

    from jarvis_common.llm_client import get_litellm_config  # noqa: PLC0415

    services: list[ServiceStatus] = []
    pool = request.app.state.db_pool
    http = request.app.state.http_client

    # PostgreSQL
    try:
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        services.append(ServiceStatus(name="postgres", ok=True))
    except Exception as exc:  # noqa: BLE001
        services.append(ServiceStatus(name="postgres", ok=False, detail=str(exc)[:200]))

    # Qdrant
    try:
        qdrant = getattr(request.app.state, "qdrant_client", None)
        if qdrant is None:
            services.append(ServiceStatus(name="qdrant", ok=False, detail="not initialised"))
        else:
            await asyncio.wait_for(qdrant.get_collections(), timeout=5.0)
            services.append(ServiceStatus(name="qdrant", ok=True))
    except Exception as exc:  # noqa: BLE001
        services.append(ServiceStatus(name="qdrant", ok=False, detail=str(exc)[:200]))

    # Ollama
    try:
        base = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        resp = await asyncio.wait_for(http.get(f"{base}/api/tags"), timeout=5.0)
        services.append(
            ServiceStatus(
                name="ollama",
                ok=resp.status_code == 200,
                detail=None if resp.status_code == 200 else f"HTTP {resp.status_code}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        services.append(ServiceStatus(name="ollama", ok=False, detail=str(exc)[:200]))

    # LiteLLM
    try:
        cfg = get_litellm_config()
        resp = await asyncio.wait_for(
            http.get(f"{cfg.base_url}/health/readiness"),
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
        services.append(ServiceStatus(name="litellm", ok=False, detail=str(exc)[:200]))

    return SystemCheckResponse(services=services, all_ok=all(s.ok for s in services))


async def _persist_config(pool: Any, key: str, value: Any, *, encrypted: bool) -> None:
    """Insert/update a single user_config row with the right column."""
    if encrypted:
        ciphertext = encrypt_secret(str(value)).encode("ascii")
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_config (key, value, encrypted_value)
                VALUES ($1, NULL, $2)
                ON CONFLICT (key) DO UPDATE
                    SET value = NULL, encrypted_value = $2, updated_at = NOW()
                """,
                key,
                ciphertext,
            )
    else:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_config (key, value)
                VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE
                    SET value = $2::jsonb, updated_at = NOW()
                """,
                key,
                json.dumps(value),
            )


async def _send_test_email(body: SmtpBody, recipient: str) -> str | None:
    """Best-effort SMTP test send. Returns None on success, error string on failure."""
    try:
        import aiosmtplib  # noqa: PLC0415
    except ImportError:
        return "aiosmtplib not installed in this image"

    message = EmailMessage()
    message["From"] = body.from_email
    message["To"] = recipient
    message["Subject"] = "JARVIS SMTP test"
    message.set_content(
        "This is a test email from the JARVIS first-run setup wizard.\n"
        "If you received this, your SMTP relay is working.\n"
    )

    use_tls = body.port == 465
    start_tls = not use_tls
    try:
        await aiosmtplib.send(
            message,
            hostname=body.host,
            port=body.port,
            username=body.user or None,
            password=body.password or None,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=SMTP_TEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("setup smtp test_send failed: %s", exc)
        return str(exc)[:300]
    return None


@router.post(
    "/smtp",
    response_model=SmtpResponse,
    dependencies=[],
)
async def configure_smtp(body: SmtpBody, request: Request) -> SmtpResponse:
    """Persist SMTP config and optionally fire a test email."""
    await require_unconfigured_or_admin(request)

    pool = request.app.state.db_pool
    await _persist_config(pool, "smtp.host", body.host, encrypted=False)
    await _persist_config(pool, "smtp.port", body.port, encrypted=False)
    if body.user is not None:
        await _persist_config(pool, "smtp.user", body.user, encrypted=False)
    await _persist_config(pool, "smtp.from", body.from_email, encrypted=False)
    if body.password:
        await _persist_config(pool, "smtp.pass", body.password, encrypted=True)

    test_sent: bool | None = None
    test_error: str | None = None
    if body.test_send:
        recipient = body.test_recipient or body.from_email
        err = await _send_test_email(body, recipient)
        test_sent = err is None
        test_error = err

    return SmtpResponse(saved=True, test_sent=test_sent, test_error=test_error)


@router.post(
    "/admin",
    response_model=AdminResponse,
    dependencies=[],
)
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
    pool = request.app.state.db_pool
    email_norm = body.email.lower().strip()
    now = datetime.now(UTC)

    async with pool.acquire() as conn:
        async with conn.transaction():
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

            session_id = await conn.fetchval(
                """
                INSERT INTO sessions (user_id, expires_at)
                VALUES ($1, $2)
                RETURNING id
                """,
                user_id,
                now + SESSION_TTL,
            )

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=str(session_id),
        max_age=int(SESSION_TTL.total_seconds()),
        expires=int((now + SESSION_TTL).timestamp()),
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path="/",
    )

    logger.info("setup: first admin created id=%s email=%s", user_id, email_norm)
    return AdminResponse(id=user_id, email=user_row["email"], role=user_row["role"])


@router.post(
    "/cloud-llm-keys",
    response_model=CloudLlmKeysResponse,
    dependencies=[],
)
async def configure_cloud_llm_keys(
    body: CloudLlmKeysBody, request: Request
) -> CloudLlmKeysResponse:
    """Persist optional cloud-LLM provider API keys (Fernet-encrypted)."""
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
        await _persist_config(pool, _CLOUD_LLM_KEY_MAP[provider], value.strip(), encrypted=True)
        saved.append(provider)

    return CloudLlmKeysResponse(saved_providers=saved)


__all__ = ["router", "require_unconfigured_or_admin"]
