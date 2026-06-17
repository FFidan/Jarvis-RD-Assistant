"""Admin user-management endpoints.

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

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from jarvis_common.audit import log_audit
from jarvis_common.auth import require_admin
from jarvis_common.email import send_magic_link
from pydantic import BaseModel, EmailStr, Field

from paper_ingestion.routers.auth import MAGIC_LINK_TTL, _hash_email, _hash_token

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


def _build_invite_link(request: Request, token: str) -> str:
    """Construct the magic-link URL for an invited user."""
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    base = get_paper_ingestion_settings().app_base_url
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

        try:
            user_row = await conn.fetchrow(
                """
                INSERT INTO users (email, role)
                VALUES ($1, $2)
                RETURNING id, email, role, created_at, last_login_at
                """,
                email_norm,
                body.role,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A removed user with that email exists — restore or purge them first.",
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
        logger.exception(
            "send_magic_link (invite) failed for email_hash=%s", _hash_email(email_norm)
        )

    caller_id = getattr(request.state, "user_id", None)
    await log_audit(
        pool,
        action="admin.user.invite",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
    )

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

        old_role: str | None = await conn.fetchval(
            "SELECT role FROM users WHERE id = $1 AND deleted_at IS NULL",
            user_id,
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

    await log_audit(
        pool,
        action="admin.user.role_change",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
        metadata={"old_role": old_role, "new_role": body.role},
    )

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

    # Soft delete only — the daily data_purge job hard-deletes after the
    # 30-day grace, and migration 080's ON DELETE CASCADE FKs then collapse
    # all owned rows.
    await log_audit(
        pool,
        action="admin.user.soft_delete",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
    )

    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/users/{user_id}/restore",
    response_model=UserRecord,
    dependencies=[Depends(require_admin)],
)
async def restore_user(user_id: int, request: Request) -> UserRecord:
    """Clear ``deleted_at`` for a soft-deleted user still within the 30-day grace.

    Raises 404 if the user does not exist, is not soft-deleted, or the grace
    window has already elapsed (the data_purge job may have hard-deleted them).
    """
    pool = request.app.state.db_pool
    caller_id: int | None = getattr(request.state, "user_id", None)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
            SET deleted_at = NULL
            WHERE id = $1
              AND deleted_at IS NOT NULL
              AND deleted_at >= NOW() - INTERVAL '30 days'
            RETURNING id, email, role, created_at, last_login_at
            """,
            user_id,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found, not deleted, or past the 30-day restore grace",
        )

    await log_audit(
        pool,
        action="admin.user.restore",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
    )

    return _row_to_user(row)


@router.post(
    "/users/{user_id}/send-link",
    dependencies=[Depends(require_admin)],
)
async def send_sign_in_link(user_id: int, request: Request) -> dict[str, bool]:
    """Email an existing user a fresh 15-minute magic sign-in link.

    Mirrors :func:`invite_user`'s token path but targets an EXISTING
    non-deleted user and uses the short ``MAGIC_LINK_TTL`` (a login token,
    not a 24h invite). ``pending_email`` is left NULL so ``/auth/verify``
    treats it as a plain login token (it rejects pending-email tokens).

    Raises 404 if the user does not exist or is soft-deleted.
    """
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, email FROM users WHERE id = $1 AND deleted_at IS NULL",
            user_id,
        )

    if user_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    email = user_row["email"]
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

    link = _build_invite_link(request, raw_token)
    try:
        await send_magic_link(email, link, pool=pool)
    except Exception:  # noqa: BLE001 — never expose SMTP errors
        logger.exception("send_magic_link (sign-in) failed for user_id=%s", user_id)

    caller_id = getattr(request.state, "user_id", None)
    await log_audit(
        pool,
        action="admin.user.send_link",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
    )

    return {"sent": True}


__all__ = ["router"]
