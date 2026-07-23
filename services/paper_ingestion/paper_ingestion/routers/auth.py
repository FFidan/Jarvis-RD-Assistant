"""Magic-link authentication endpoints.

Three endpoints:

- ``POST /api/auth/request-link`` — body ``{email}`` → always returns
  ``{"sent": true}``. If the email belongs to a real user, a one-shot
  15-minute magic-link is generated and emailed when SMTP is available. If it
  cannot be delivered, the bearer link is dropped rather than logged. Unknown
  emails get the same response shape (don't leak which emails exist).
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
import ipaddress
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status
from jarvis_common.audit import log_audit
from jarvis_common.auth import RAW_CLIENT_SCOPE_KEY
from jarvis_common.email import MagicLinkDelivery, send_magic_link
from jarvis_common.event_log import log_event
from jarvis_common.owner import OwnerIdentity, resolve_owner_identity
from jarvis_common.session_middleware import SESSION_COOKIE_NAME, mint_session
from jarvis_common.settings import get_core_settings
from pydantic import BaseModel, EmailStr, Field

from paper_ingestion.deps import limiter
from paper_ingestion.routers._auth_shared import build_verify_link, magic_link_on_cooldown

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Exempt from the app-level verify_api_key by the `/api/auth/` path check inside
# it — this router IS the auth bootstrap, so it cannot require a credential the
# caller has not been issued yet. Each endpoint enforces its own token TTL and
# single-use semantics.

MAGIC_LINK_TTL = timedelta(minutes=15)
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
    return build_verify_link(request, token, logger=logger, link_kind="magic link")


async def _deliver_requested_magic_link(email: str, link: str, pool: Any) -> None:
    """Deliver a persisted login link after its request response has been sent."""
    try:
        result = await send_magic_link(email, link, pool=pool)
    except Exception:  # noqa: BLE001 — the background task must not leak SMTP detail
        logger.exception("send_magic_link failed for email_hash=%s", _hash_email(email))
        result = MagicLinkDelivery.FAILED
    if result is MagicLinkDelivery.FAILED:
        # Best-effort and PII-free: delivery failure is visible to the operator,
        # while neither the raw recipient nor the bearer-token link is recorded.
        with contextlib.suppress(Exception):
            await log_event(
                pool=pool,
                level="warning",
                category="auth",
                source="auth",
                message="magic_link_send_failed",
                context={"email_hash": _hash_email(email)},
            )


def _ip_in_cidrs(value: str | None, cidrs: list[str]) -> bool:
    """Return whether a concrete peer address belongs to an explicit allowlist."""
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    for index, cidr in enumerate(cidrs):
        if cidr == "*":
            continue
        try:
            if address in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            # Log the position, not the entry: the allowlist is operator-controlled
            # config, and keeping its raw value out of the log record avoids a
            # clear-text-logging finding while still pointing at the bad element.
            logger.warning("Ignoring malformed transport-allowlist CIDR at index %d", index)
    return False


def _raw_peer_host(request: Request) -> str | None:
    """Read the socket peer captured before proxy-header rewriting."""
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict) and RAW_CLIENT_SCOPE_KEY in scope:
        raw = scope.get(RAW_CLIENT_SCOPE_KEY)
        if isinstance(raw, (tuple, list)) and raw and isinstance(raw[0], str):
            return raw[0]
        return None
    # Direct handler unit tests may not install the app middleware. Production
    # requests always take the stashed branch above.
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def _require_local_or_https(request: Request) -> None:
    """Permit credential exchange only through a verifiable transport path.

    Host and forwarding headers are client-controlled and are never consulted
    here. ``RawClientStashMiddleware`` identifies the actual dashboard proxy;
    only that pinned peer may convey the rewritten HTTPS scheme or the small
    host-local HTTP allowlist.
    """
    settings = get_core_settings()
    raw_peer = _raw_peer_host(request)
    if _ip_in_cidrs(raw_peer, ["127.0.0.0/8", "::1/128"]):
        return

    if _ip_in_cidrs(raw_peer, settings.trusted_proxy_hosts_list):
        scheme = str(getattr(getattr(request, "url", None), "scheme", "")).lower()
        if scheme == "https":
            return
        client = getattr(request, "client", None)
        client_host = getattr(client, "host", None)
        if scheme == "http" and _ip_in_cidrs(client_host, settings.trusted_local_client_cidrs_list):
            return

    raise HTTPException(
        status_code=403,
        detail=(
            "This address is for diagnostics only. Open JARVIS through verified "
            "HTTPS, or use http://localhost on the server, before entering a "
            "setup token, sign-in link, or API key."
        ),
    )


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
async def request_link(
    body: RequestLinkBody,
    request: Request,
    background_tasks: BackgroundTasks,
) -> RequestLinkResponse:
    """Issue a magic-link for ``body.email`` if the email matches a known user.

    Always returns ``{"sent": true}`` regardless of whether the email exists,
    with SMTP delivery deferred until after the response so relay latency cannot
    reveal valid accounts.

    When SMTP is unconfigured (no relay in DB or env) the link is silently
    dropped after token generation: ``send_magic_link`` takes the dev-mode
    fallback that records only a SHA-256 hash of the email in ``system_events``
    (visible in Logs Live) without delivering the link anywhere.  The response
    is still ``{"sent": true}`` so enumeration resistance is preserved — callers
    MUST use the ``smtp_configured`` field on ``GET /api/setup/status`` to
    surface this condition in the UI before the user submits an email.
    """
    _require_local_or_https(request)
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
        # Equalise bounded application-side work without scheduling delivery:
        # hash a throwaway token and mirror the known branch's second pool
        # acquire. SMTP never runs on either request path.
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
        if await magic_link_on_cooldown(conn, user_id, email_change=False):
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

    background_tasks.add_task(
        _deliver_requested_magic_link,
        email_norm,
        _build_magic_link(request, raw_token),
        pool,
    )

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
    _require_local_or_https(request)
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


def _owner_configuration_conflict(identity: OwnerIdentity) -> HTTPException:
    """Build an actionable error for an authoritative but unusable owner."""
    if identity.source == "environment":
        if identity.state == "invalid_value":
            problem = "OWNER_USER_ID must be a positive integer"
        elif identity.state == "non_admin_user":
            problem = "OWNER_USER_ID references a non-admin user"
        else:
            problem = "OWNER_USER_ID references a missing or deleted user"
        return HTTPException(
            status_code=409,
            detail=f"{problem}; correct OWNER_USER_ID on the host and restart JARVIS",
        )

    if identity.state == "invalid_value":
        problem = "the stored owner record is not a positive integer"
    elif identity.state == "non_admin_user":
        problem = "the stored owner record references a non-admin user"
    else:
        problem = "the stored owner record references a missing or deleted user"
    return HTTPException(
        status_code=409,
        detail=f"{problem}; repair it with jarvis-research owner set <admin-email>",
    )


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

    1. Bind to ONE explicit owner when configured. A single-user install may
       fall back to its only live admin. Never create or silently substitute a
       user when an authoritative owner setting is invalid.
    2. Audit + rate-limit — ``auth.api_key.session.minted`` on success,
       ``auth.api_key.session.failure`` on bad key, via the same ``log_audit``
       helper magic-link uses; ``@limiter.limit`` matches the tighter auth
       limit (verify = 10/minute).
    3. Single-tenant gate — mints only when exactly one non-deleted user
       exists OR ``API_KEY_LOGIN_ENABLED`` is true; otherwise 403 telling the
       caller to use magic-link.
    """
    _require_local_or_https(request)

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
        async with conn.transaction():
            # Coordinate with owner transfer, owner repair, and admin role/delete
            # mutations. The final lookup and session insert therefore observe
            # one stable owner identity for every cooperating writer.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('admin_role_mutation'))")
            owner_identity = await resolve_owner_identity(conn)

            if owner_identity.source != "none" and not owner_identity.is_valid:
                raise _owner_configuration_conflict(owner_identity)

            user_count = await conn.fetchval("SELECT count(*) FROM users WHERE deleted_at IS NULL")
            multi_user = int(user_count or 0) != 1
            flag_enabled = await api_key_login_enabled(conn)
            owner_exempt = owner_identity.is_valid

            if multi_user and not flag_enabled and not owner_exempt:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "API-key recovery is reserved for the configured instance owner; "
                        "use a passkey or sign-in link"
                    ),
                )

            if multi_user and flag_enabled and not owner_exempt:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "set OWNER_USER_ID (or create the first admin via the setup "
                        "wizard) for multi-user API-key login; refusing to bind the "
                        "session to an arbitrary admin"
                    ),
                )

            owner: Any
            if owner_identity.is_valid:
                owner = await conn.fetchrow(
                    "SELECT id, email, role FROM users "
                    "WHERE id = $1 AND deleted_at IS NULL AND role = 'admin'",
                    owner_identity.user_id,
                )
                if owner is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "the configured owner changed while sign-in was in progress; "
                            "retry after repairing the owner configuration"
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

    # SessionMiddleware.dispatch re-issues a fresh 30-day cookie AFTER the route
    # when request.state.session_renewed is truthy (set before logout ran, for a
    # renewal-eligible session). Clear it here so that re-issue is skipped and does
    # not clobber the deletion below. Unconditional + idempotent (falsy → no-op).
    request.state.session_renewed = None
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
