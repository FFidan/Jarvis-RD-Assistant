"""Magic-link authentication endpoints.

Three endpoints:

- ``POST /api/auth/request-link`` — body ``{email}`` → always returns
  ``{"sent": true}``. If the email belongs to a real user, a one-shot
  15-minute magic-link is generated and either emailed or logged to stdout
  in dev-mode. Unknown emails get the same response shape (don't leak which
  emails exist).
- ``POST /api/auth/verify`` — body ``{token}`` → looks up SHA-256(token) in
  ``magic_link_tokens``. Rejects on missing/expired/already-used. Otherwise
  marks ``used_at``, creates a 30-day session row, sets the ``jarvis_session``
  cookie (HttpOnly, SameSite=Strict, Secure outside DEV_MODE), and returns
  the user record.
- ``POST /api/auth/logout`` — sets ``revoked_at`` on the current session and
  clears the cookie. 204.

These endpoints are exempt from ``verify_api_key`` (registered without auth
dependency) since they ARE the auth bootstrap.
"""

import contextlib
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from jarvis_common.audit import log_audit
from jarvis_common.email import send_magic_link
from jarvis_common.event_log import log_event
from jarvis_common.owner import resolve_owner_user_id
from jarvis_common.session_middleware import SESSION_COOKIE_NAME, mint_session
from jarvis_common.settings import get_core_settings
from pydantic import BaseModel, EmailStr, Field

from paper_ingestion.deps import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Auth router endpoints are exempt from the global verify_api_key dependency.
# They're explicitly registered with `dependencies=[]` overrides at include time
# (see main.py). Marker attribute so future linters can audit.
router.auth_exempt = True  # type: ignore[attr-defined]

MAGIC_LINK_TTL = timedelta(minutes=15)
MAGIC_LINK_COOLDOWN = timedelta(minutes=2)
MAX_EMAIL_LEN = 320  # RFC 5321 cap


class RequestLinkBody(BaseModel):
    email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LEN)]


class RequestLinkResponse(BaseModel):
    sent: bool


class VerifyBody(BaseModel):
    token: Annotated[str, Field(min_length=16, max_length=128)]


class UserResponse(BaseModel):
    id: int
    email: str
    role: str


def _hash_token(token: str) -> str:
    """Return the canonical token-storage form (hex SHA-256)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_email(email: str) -> str:
    """Pseudonymise an email for audit ``resource``. Never log the raw value."""
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()


def _audit_pool(request: Request):
    """Best-effort ``app.state.db_pool`` lookup tolerant of test mocks.

    Mirrors ``jarvis_common.auth._request_db_pool``: audit logging must never
    blow up the auth flow just because a test request stub has no real pool.
    """
    state = getattr(getattr(request, "app", None), "state", None)
    return getattr(state, "db_pool", None) if state is not None else None


def _build_magic_link(request: Request, token: str) -> str:
    """Construct the URL the user clicks. Honours X-Forwarded-* via ProxyHeadersMiddleware."""
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    base = get_paper_ingestion_settings().app_base_url
    if base:
        return f"{base.rstrip('/')}/auth/verify?token={token}"
    # Fallback: derive from the incoming request. ProxyHeadersMiddleware has
    # already substituted the public scheme/host before this code runs.
    return str(request.url.replace(path="/auth/verify", query=f"token={token}"))


def _cookie_secure() -> bool:
    """Match the Secure flag to the runtime mode.

    DEV_MODE=true → Secure=False so localhost http://… still works.
    Anything else → Secure=True (HSTS-friendly, prevents downgrade leak).
    """
    return not get_core_settings().dev_mode


@router.post(
    "/request-link",
    response_model=RequestLinkResponse,
    dependencies=[],  # exempt from verify_api_key
)
@limiter.limit("5/minute")
async def request_link(body: RequestLinkBody, request: Request) -> RequestLinkResponse:
    """Issue a magic-link for ``body.email`` if the email matches a known user.

    Always returns ``{"sent": true}`` regardless of whether the email exists,
    so an attacker cannot enumerate valid accounts by timing/response shape.

    When SMTP is unconfigured (no relay in DB or env) the link is silently
    dropped after token generation: ``send_magic_link`` takes the dev-mode
    fallback that records only a SHA-256 hash of the email in ``system_events``
    (visible in Logs Live) without delivering the link anywhere.  The response
    is still ``{"sent": true}`` so enumeration resistance is preserved — callers
    MUST use the ``smtp_configured`` field on ``GET /api/setup/status`` to
    surface this condition in the UI before the user submits an email.
    """
    pool = request.app.state.db_pool
    email_norm = body.email.lower().strip()

    _audit = _audit_pool(request)
    if _audit is not None:
        await log_audit(
            _audit,
            action="auth.magic_link.request",
            resource=_hash_email(email_norm),
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1 AND deleted_at IS NULL",
            email_norm,
        )

    if row is None:
        # Don't leak account existence. Equalise the application-side work the
        # known-email branch does so there is no trivial fast/slow timing split:
        # always mint + SHA-256-hash a throwaway token (the CPU oracle) and
        # always perform an equivalent connection-acquire round-trip (mirrors
        # the known branch's second pool.acquire). We deliberately do NOT
        # INSERT or call send_magic_link — the DB write + SMTP send cannot be
        # safely faked and true constant-time across real SMTP is infeasible;
        # this closes the order-of-magnitude split, not the residual µs-level
        # one, which is the accepted bar here.
        _hash_token(secrets.token_urlsafe(32))  # decoy: equalise CPU work
        async with pool.acquire():
            pass
        logger.info("auth_request_link_unknown_email email_hash=%s", _hash_email(email_norm))
        return RequestLinkResponse(sent=True)

    user_id = int(row["id"])
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + MAGIC_LINK_TTL

    async with pool.acquire() as conn:
        recent = await conn.fetchval(
            "SELECT created_at FROM magic_link_tokens"
            " WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id,
        )
        if (
            recent is not None
            and datetime.now(UTC) - recent.replace(tzinfo=UTC) < MAGIC_LINK_COOLDOWN
        ):
            logger.info("auth_request_link_cooldown email_hash=%s", _hash_email(email_norm))
            return RequestLinkResponse(sent=True)

        await conn.execute(
            """
            INSERT INTO magic_link_tokens (token_hash, user_id, expires_at)
            VALUES ($1, $2, $3)
            """,
            token_hash,
            user_id,
            expires_at,
        )

    link = _build_magic_link(request, raw_token)
    try:
        await send_magic_link(email_norm, link, pool=pool)
    except Exception:  # noqa: BLE001 — never leak SMTP detail to the response
        logger.exception("send_magic_link failed for email_hash=%s", _hash_email(email_norm))
        # Record a server-side event so an operator can see the outage in Logs
        # Live. Suppress ANY error from the write: this unauthenticated path MUST
        # always return sent=True (its shape + timing are the anti-enumeration
        # defense), and the event is best-effort. The payload carries only a
        # PII-free email hash.
        with contextlib.suppress(Exception):
            await log_event(
                pool=pool,
                level="warning",
                category="auth",
                source="auth",
                message="magic_link_send_failed",
                context={"email_hash": _hash_email(email_norm)},
            )
        # Still return sent=true: the user can re-request, and we don't want
        # the response to advertise SMTP outage to unauthenticated callers.

    return RequestLinkResponse(sent=True)


@router.post(
    "/verify",
    response_model=UserResponse,
    dependencies=[],
)
@limiter.limit("10/minute")
async def verify(
    body: VerifyBody,
    request: Request,
    response: Response,
) -> UserResponse:
    """Exchange a magic-link token for a session cookie.

    Atomicity matters: token consumption + session creation must succeed or
    fail as a unit so a token cannot be replayed if the session insert fails.
    """
    pool = request.app.state.db_pool
    token_hash = _hash_token(body.token)
    now = datetime.now(UTC)

    _audit = _audit_pool(request)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                token_row = await conn.fetchrow(
                    """
                    SELECT user_id, expires_at, used_at, pending_email
                    FROM magic_link_tokens
                    WHERE token_hash = $1
                    FOR UPDATE
                    """,
                    token_hash,
                )
                if token_row is None:
                    raise HTTPException(status_code=400, detail="Invalid or expired token")
                if token_row["used_at"] is not None:
                    raise HTTPException(status_code=400, detail="Invalid or expired token")
                if token_row["expires_at"] <= now:
                    raise HTTPException(status_code=400, detail="Invalid or expired token")
                # Email-change confirmation tokens (pending_email set) are NOT
                # login tokens. Accepting one here would mint a 30-day session
                # cookie from a token meant only for /account/confirm-email — a
                # passwordless-login bypass. Symmetric counterpart of the
                # ``pending_email is None`` guard in confirm_email_change.
                if token_row["pending_email"] is not None:
                    raise HTTPException(status_code=400, detail="Invalid or expired token")

                user_id = int(token_row["user_id"])

                user_row = await conn.fetchrow(
                    "SELECT id, email, role, deleted_at FROM users WHERE id = $1",
                    user_id,
                )
                if user_row is None or user_row["deleted_at"] is not None:
                    raise HTTPException(status_code=400, detail="Invalid or expired token")

                await conn.execute(
                    "UPDATE magic_link_tokens SET used_at = NOW() WHERE token_hash = $1",
                    token_hash,
                )
                await conn.execute(
                    "UPDATE users SET last_login_at = NOW() WHERE id = $1",
                    user_id,
                )

                await mint_session(conn, response, user_id, now=now)
    except HTTPException:
        if _audit is not None:
            ua = request.headers.get("user-agent", "") if hasattr(request, "headers") else ""
            await log_audit(
                _audit,
                action="auth.magic_link.verify.failure",
                resource="magic_link_token",
                metadata={
                    "ip": request.client.host if request.client else None,
                    "ua_prefix": ua[:80],
                },
            )
        raise

    if _audit is not None:
        await log_audit(
            _audit,
            action="auth.magic_link.verify.success",
            resource="magic_link_token",
            user_id=str(user_id),
        )

    return UserResponse(
        id=user_id,
        email=user_row["email"],
        role=user_row["role"],
    )


class ApiKeySessionBody(BaseModel):
    """Body for ``POST /api/auth/api-key-session``.

    The key may also be supplied via the ``X-API-Key`` header (the path the
    frontend already uses); the body field is optional so either works.
    """

    api_key: Annotated[str, Field(min_length=1, max_length=512)] | None = None


def _submitted_api_key(body: ApiKeySessionBody, request: Request) -> str:
    """Resolve the submitted key: JSON body first, then ``X-API-Key`` header."""
    if body.api_key:
        return body.api_key
    headers = getattr(request, "headers", None)
    if headers is not None:
        return headers.get("X-API-Key", "") or ""
    return ""


@router.post(
    "/api-key-session",
    response_model=UserResponse,
    dependencies=[],  # exempt from verify_api_key (it IS the auth bootstrap)
)
@limiter.limit("10/minute")
async def api_key_session(
    body: ApiKeySessionBody,
    request: Request,
    response: Response,
) -> UserResponse:
    """Exchange a valid ``JARVIS_API_KEY`` for a real owner-scoped session.

    API-key-to-session exchange (decision A2). Three non-negotiable guardrails:

    1. Bind to ONE explicit owner — ``OWNER_USER_ID`` setting if present,
       else the lowest-id non-deleted ``role='admin'`` user. Never a
       synthetic / shared / auto-created user. No admin → 409 (no creation).
    2. Audit + rate-limit — ``auth.api_key.session.minted`` on success,
       ``auth.api_key.session.failure`` on bad key, via the same ``log_audit``
       helper magic-link uses; ``@limiter.limit`` matches the tighter auth
       limit (verify = 10/minute).
    3. Single-tenant gate — mints only when exactly one non-deleted user
       exists OR ``API_KEY_LOGIN_ENABLED`` is true; otherwise 403 telling the
       caller to use magic-link.
    """
    from jarvis_common.auth import _CACHED_API_KEY  # noqa: PLC0415

    pool = request.app.state.db_pool
    now = datetime.now(UTC)
    _audit = _audit_pool(request)
    submitted = _submitted_api_key(body, request)

    # Guardrail 2 (failure half): validate the key with hmac.compare_digest
    # exactly as verify_api_key does. No configured key → cannot mint.
    if not _CACHED_API_KEY or not hmac.compare_digest(
        submitted.encode("utf-8", errors="replace"),
        _CACHED_API_KEY.encode("utf-8", errors="replace"),
    ):
        if _audit is not None:
            ua = request.headers.get("user-agent", "") if hasattr(request, "headers") else ""
            await log_audit(
                _audit,
                action="auth.api_key.session.failure",
                resource="api_key_session",
                metadata={
                    "ip": request.client.host if request.client else None,
                    "ua_prefix": ua[:80],
                },
            )
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

    from jarvis_common.auth import api_key_login_enabled  # noqa: PLC0415

    async with pool.acquire() as conn:
        resolved_owner_id = await resolve_owner_user_id(conn)
        # Guardrail 3: single-tenant gate.
        user_count = await conn.fetchval("SELECT count(*) FROM users WHERE deleted_at IS NULL")
        multi_user = int(user_count or 0) != 1
        flag_enabled = await api_key_login_enabled(conn)

        # B1 (owner self-lockout exemption): a configured OWNER_USER_ID that
        # resolves to a live admin can ALWAYS mint via API key — even on a
        # multi-user box with the flag OFF — so the operator is never locked out
        # once magic-link is the only path and SMTP is down. Pre-resolve with the
        # SAME role='admin' lookup the explicit-owner branch uses; members never
        # match (the lookup requires admin), so this exempts the owner only.
        owner: Any = None
        if resolved_owner_id is not None:
            owner = await conn.fetchrow(
                "SELECT id, email, role FROM users "
                "WHERE id = $1 AND deleted_at IS NULL AND role = 'admin'",
                int(resolved_owner_id),
            )
        owner_exempt = owner is not None

        if multi_user and not flag_enabled and not owner_exempt:
            raise HTTPException(
                status_code=403,
                detail=("API-key login disabled for multi-tenant deployments; use magic-link"),
            )

        # Fallback gate: when the gate passed ONLY because the flag is on
        # (multi-user box, flag enabled, no exempt owner), an explicit
        # OWNER_USER_ID is mandatory — binding the session to an arbitrary
        # lowest-id admin on a shared box is a silent privilege grant. Refuse the
        # fallback; the explicit-owner branch below handles a set-but-unresolved
        # OWNER_USER_ID with its own 409s. Single-user keeps the fallback.
        if multi_user and flag_enabled and not owner_exempt and resolved_owner_id is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "set OWNER_USER_ID (or create the first admin via the setup "
                    "wizard) for multi-user API-key login; refusing to bind the "
                    "session to an arbitrary admin"
                ),
            )

        # Guardrail 1: resolve the single explicit owner. Never create one.
        if resolved_owner_id is not None:
            # A-3 (defense-in-depth): the explicit-owner lookup (pre-resolved as
            # ``owner`` above) enforces role='admin' symmetrically with the
            # fallback branch below. A configured owner that resolves to a
            # non-admin must NOT silently mint a member "owner" session —
            # distinguish "missing" (deleted/absent) from "exists but not admin"
            # so the operator gets an actionable error instead of a privilege
            # downgrade. The owner is sourced from either OWNER_USER_ID env or the
            # first-admin owner record, so both are named in the errors below.
            if owner is None:
                non_admin = await conn.fetchrow(
                    "SELECT id FROM users WHERE id = $1 AND deleted_at IS NULL",
                    int(resolved_owner_id),
                )
                if non_admin is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "the configured owner (OWNER_USER_ID env or the "
                            "first-admin owner record) references a non-admin "
                            "user; no session minted (promote the user to admin "
                            "or set OWNER_USER_ID to a live admin)"
                        ),
                    )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "the configured owner (OWNER_USER_ID env or the "
                        "first-admin owner record) references a missing or "
                        "deleted user; no session minted (no user is created)"
                    ),
                )
        else:
            owner = await conn.fetchrow(
                "SELECT id, email, role FROM users "
                "WHERE role = 'admin' AND deleted_at IS NULL "
                "ORDER BY id ASC LIMIT 1"
            )
            if owner is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "No admin user exists; cannot mint an API-key session "
                        "(no user is created — provision an admin first)"
                    ),
                )

        owner_id = int(owner["id"])
        await mint_session(conn, response, owner_id, now=now)

    if _audit is not None:
        await log_audit(
            _audit,
            action="auth.api_key.session.minted",
            resource="api_key_session",
            user_id=str(owner_id),
            metadata={
                "ip": request.client.host if request.client else None,
            },
        )

    return UserResponse(
        id=owner_id,
        email=owner["email"],
        role=owner["role"],
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[],
)
@limiter.limit("30/minute")
async def logout(request: Request, response: Response) -> Response:
    """Revoke the current session and clear the cookie.

    Idempotent: missing cookie or unknown session both return 204 with the
    cookie cleared.
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        try:
            session_id = uuid.UUID(cookie)
        except ValueError:
            session_id = None
        if session_id is not None:
            pool = request.app.state.db_pool
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE sessions SET revoked_at = NOW()
                    WHERE id = $1 AND revoked_at IS NULL
                    """,
                    str(session_id),
                )
            _audit = _audit_pool(request)
            if _audit is not None:
                state = getattr(request, "state", None)
                uid = getattr(state, "user_id", None) if state is not None else None
                await log_audit(
                    _audit,
                    action="auth.logout",
                    resource="session",
                    user_id=str(uid) if isinstance(uid, int) else None,
                )

    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


__all__ = ["router"]
