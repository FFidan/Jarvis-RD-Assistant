"""Admin user-management endpoints (Phase 2 WS-2B).

All endpoints require both:
1. A valid session cookie (set by SessionMiddleware on the request) OR
   an X-API-Key that satisfies ``verify_api_key`` (already enforced globally).
2. ``role = 'admin'`` on the resolved session user (enforced by
   ``require_admin`` below).

Registered in main.py with ``dependencies=[]`` at include time to override
the global ``verify_api_key`` — the same exemption shape used by auth.py.
Session-only callers (browsers logged in via magic-link) do NOT need to send
``X-API-Key``; the session cookie is sufficient.

Endpoints
---------
GET  /api/admin/users             — list all non-deleted users
POST /api/admin/users             — invite a new user (creates row + magic link)
PATCH /api/admin/users/{id}/role  — change role; cannot demote yourself if last admin
DELETE /api/admin/users/{id}      — soft-delete; cannot delete yourself
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from jarvis_common.email import send_magic_link
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Mark as session-exempt from global verify_api_key (browser session is enough).
router.auth_exempt = True  # type: ignore[attr-defined]

# Invite tokens get a longer TTL than normal 15-min magic links because the
# recipient may not check email immediately.
INVITE_TOKEN_TTL = timedelta(hours=24)

MAX_EMAIL_LEN = 320  # RFC 5321


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class UserRecord(BaseModel):
    id: int
    email: str
    role: str
    created_at: datetime
    last_login_at: datetime | None


class InviteUserBody(BaseModel):
    email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LEN)]
    role: Annotated[str, Field(pattern="^(user|admin)$")]


class UpdateRoleBody(BaseModel):
    role: Annotated[str, Field(pattern="^(user|admin)$")]


# ---------------------------------------------------------------------------
# Admin dependency
# ---------------------------------------------------------------------------


async def require_admin(request: Request) -> None:
    """Raise HTTP 403 if the caller is not authenticated with admin role.

    Reads ``request.state.user_role`` set by SessionMiddleware.  Missing state
    (no session cookie) also yields 403 — this endpoint requires a real browser
    session with admin role, not just a raw API key, because raw-API-key callers
    are not associated with a user row.
    """
    role = getattr(request.state, "user_role", None)
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _build_invite_link(request: Request, token: str) -> str:
    """Construct the magic-link URL for an invited user."""
    base = os.environ.get("APP_BASE_URL")
    if base:
        return f"{base.rstrip('/')}/auth/verify?token={token}"
    return str(request.url.replace(path="/auth/verify", query=f"token={token}"))


def _row_to_user(row: dict) -> UserRecord:  # type: ignore[type-arg]
    return UserRecord(
        id=int(row["id"]),
        email=row["email"],
        role=row["role"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    response_model=list[UserRecord],
    dependencies=[Depends(require_admin)],
)
async def list_users(request: Request) -> list[UserRecord]:
    """Return all non-deleted users ordered by creation date."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, role, created_at, last_login_at
            FROM users
            WHERE deleted_at IS NULL
            ORDER BY created_at ASC
            """,
        )
    return [_row_to_user(row) for row in rows]


@router.post(
    "/users",
    response_model=UserRecord,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def invite_user(body: InviteUserBody, request: Request) -> UserRecord:
    """Create a new user and send them a 24-hour invite magic link.

    Raises 409 if a non-deleted user with the same email already exists.
    """
    pool = request.app.state.db_pool
    email_norm = body.email.lower().strip()

    async with pool.acquire() as conn:
        # Conflict check: existing non-deleted user with same email.
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1 AND deleted_at IS NULL",
            email_norm,
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with that email already exists",
            )

        user_row = await conn.fetchrow(
            """
            INSERT INTO users (email, role)
            VALUES ($1, $2)
            RETURNING id, email, role, created_at, last_login_at
            """,
            email_norm,
            body.role,
        )

    user_id = int(user_row["id"])

    # Issue invite token with 24-hour expiry.
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + INVITE_TOKEN_TTL

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

    link = _build_invite_link(request, raw_token)
    try:
        await send_magic_link(email_norm, link, pool=pool)
    except Exception:  # noqa: BLE001 — never expose SMTP errors
        logger.exception("send_magic_link (invite) failed for email=%s", email_norm)

    return _row_to_user(user_row)


@router.patch(
    "/users/{user_id}/role",
    response_model=UserRecord,
    dependencies=[Depends(require_admin)],
)
async def update_user_role(user_id: int, body: UpdateRoleBody, request: Request) -> UserRecord:
    """Change a user's role.

    Raises 400 if the caller is trying to demote themselves and they are the
    last remaining admin (would lock out all admins).
    Raises 404 if the user does not exist or is soft-deleted.
    """
    pool = request.app.state.db_pool
    caller_id: int | None = getattr(request.state, "user_id", None)

    async with pool.acquire() as conn:
        # Self-demotion guard: if caller is demoting themselves, check admin count.
        if caller_id is not None and caller_id == user_id and body.role != "admin":
            admin_count: int = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND deleted_at IS NULL",
            )
            if admin_count <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot demote yourself — you are the last admin",
                )

        row = await conn.fetchrow(
            """
            UPDATE users
            SET role = $1
            WHERE id = $2 AND deleted_at IS NULL
            RETURNING id, email, role, created_at, last_login_at
            """,
            body.role,
            user_id,
        )

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return _row_to_user(row)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    response_model=None,
)
async def soft_delete_user(user_id: int, request: Request, response: Response) -> Response:
    """Soft-delete a user by setting ``deleted_at``.

    Raises 400 if the caller tries to delete themselves.
    Raises 404 if the user does not exist or is already deleted.
    """
    caller_id: int | None = getattr(request.state, "user_id", None)

    if caller_id is not None and caller_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE users
            SET deleted_at = NOW()
            WHERE id = $1 AND deleted_at IS NULL
            """,
            user_id,
        )

    # asyncpg returns "UPDATE N" — check the count.
    if result == "UPDATE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    response.status_code = status.HTTP_204_NO_CONTENT
    return response


__all__ = ["router"]
