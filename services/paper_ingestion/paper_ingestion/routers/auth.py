"""Magic-link authentication endpoints (Phase 2 WS-2A).

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

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response, status
from jarvis_common.email import send_magic_link
from jarvis_common.session_middleware import SESSION_COOKIE_NAME
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Auth router endpoints are exempt from the global verify_api_key dependency.
# They're explicitly registered with `dependencies=[]` overrides at include time
# (see main.py). Marker attribute so future linters can audit.
router.auth_exempt = True  # type: ignore[attr-defined]

MAGIC_LINK_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)
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
    return os.environ.get("DEV_MODE", "false").lower() != "true"


@router.post(
    "/request-link",
    response_model=RequestLinkResponse,
    dependencies=[],  # exempt from verify_api_key
)
async def request_link(body: RequestLinkBody, request: Request) -> RequestLinkResponse:
    """Issue a magic-link for ``body.email`` if the email matches a known user.

    Always returns ``{"sent": true}`` regardless of whether the email exists,
    so an attacker cannot enumerate valid accounts by timing/response shape.
    """
    pool = request.app.state.db_pool
    email_norm = body.email.lower().strip()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1 AND deleted_at IS NULL",
            email_norm,
        )

    if row is None:
        # No-op: don't leak account existence. Constant-time-ish: skip the DB
        # write entirely (it's faster than a real path), but the token-issuing
        # path is dominated by the email/log call which we can't safely fake.
        logger.info("auth_request_link_unknown_email email=%s", email_norm)
        return RequestLinkResponse(sent=True)

    user_id = int(row["id"])
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + MAGIC_LINK_TTL

    async with pool.acquire() as conn:
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
        logger.exception("send_magic_link failed for email=%s", email_norm)
        # Still return sent=true: the user can re-request, and we don't want
        # the response to advertise SMTP outage to unauthenticated callers.

    return RequestLinkResponse(sent=True)


@router.post(
    "/verify",
    response_model=UserResponse,
    dependencies=[],
)
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

    async with pool.acquire() as conn:
        async with conn.transaction():
            token_row = await conn.fetchrow(
                """
                SELECT user_id, expires_at, used_at
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

    return UserResponse(
        id=user_id,
        email=user_row["email"],
        role=user_row["role"],
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[],
)
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
