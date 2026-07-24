"""Admin user-management endpoints.

All endpoints require both:
1. Passing the app-level ``verify_api_key`` front door: it returns early once
   SessionMiddleware has set ``request.state.user_id`` from a valid session,
   so a logged-in browser needs no ``X-API-Key``; other callers must present
   a matching key.
2. ``role = 'admin'`` on the resolved session user (enforced by
   ``require_admin`` on every route below). The ops X-API-Key alone never
   satisfies this — it carries no session role.

Endpoints
---------
GET  /api/admin/users             — list all non-deleted users
POST /api/admin/users             — invite a new user (creates row + magic link)
PATCH /api/admin/users/{id}/role  — change role; cannot demote the last admin
DELETE /api/admin/users/{id}      — soft-delete; not yourself, not the last admin
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from jarvis_common.audit import log_audit, log_audit_strict
from jarvis_common.auth import require_admin
from jarvis_common.email import MagicLinkDelivery, send_magic_link
from jarvis_common.owner import (
    OWNER_USER_ID_CONFIG_KEY,
    OwnerIdentity,
    resolve_owner_identity,
)
from jarvis_common.settings import get_secrets_settings
from pydantic import BaseModel, EmailStr, Field

from paper_ingestion.models.account import MAX_EMAIL_LEN
from paper_ingestion.routers._auth_shared import build_verify_link
from paper_ingestion.routers.auth import MAGIC_LINK_TTL, _hash_email, _hash_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Invite tokens get a longer TTL than normal 15-min magic links because the
# recipient may not check email immediately.
INVITE_TOKEN_TTL = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class UserRecord(BaseModel):
    id: int
    email: str
    role: str
    created_at: datetime
    last_login_at: datetime | None
    deleted_at: datetime | None = None
    is_owner: bool = False
    owner_source: str | None = None
    owner_state: str | None = None


class InviteUserResponse(UserRecord):
    """Invite result. ``invite_link`` is populated only when SMTP could not
    deliver the magic link, so the admin can hand it over manually."""

    invite_link: str | None = None


class SendLinkResponse(BaseModel):
    """Send-link result. ``sent_link`` is populated only when SMTP could not
    deliver the sign-in link, so the admin can hand it over manually."""

    sent: bool
    sent_link: str | None = None


class InviteUserBody(BaseModel):
    email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LEN)]
    role: Annotated[str, Field(pattern="^(user|admin)$")]


class UpdateRoleBody(BaseModel):
    role: Annotated[str, Field(pattern="^(user|admin)$")]


class TransferOwnerBody(BaseModel):
    target_user_id: Annotated[int, Field(gt=0)]
    confirmation: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LEN)]


class OwnerIdentityResponse(BaseModel):
    source: str
    state: str
    user_id: int | None


def _build_invite_link(request: Request, token: str) -> str:
    """Construct the magic-link URL for an invited user."""
    return build_verify_link(request, token, logger=logger, link_kind="invite link")


def _row_to_user(
    row: dict,  # type: ignore[type-arg]
    owner: OwnerIdentity | None = None,
) -> UserRecord:
    user_id = int(row["id"])
    return UserRecord(
        id=user_id,
        email=row["email"],
        role=row["role"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        deleted_at=row.get("deleted_at"),
        is_owner=bool(owner and owner.is_valid and owner.user_id == user_id),
        owner_source=owner.source if owner else None,
        owner_state=owner.state if owner else None,
    )


def _owner_change_blocked(identity: OwnerIdentity, user_id: int) -> bool:
    return identity.is_valid and identity.user_id == user_id


def _owner_repair_blocked(identity: OwnerIdentity, user_id: int) -> bool:
    return identity.source != "none" and not identity.is_valid and identity.user_id == user_id


def _owner_repair_detail(identity: OwnerIdentity) -> str:
    if identity.source == "environment":
        return "Correct OWNER_USER_ID on the host and restart JARVIS before changing this user"
    return (
        "Repair the instance owner with jarvis-research owner set <admin-email> "
        "before changing this user"
    )


def _require_model_hmac_key() -> None:
    """Reject an operation that would make the box multi-user while the Pulse
    model-signing key is still derived from JARVIS_API_KEY."""
    _hmac = get_secrets_settings().jarvis_model_hmac_key
    if _hmac is None or len(_hmac.get_secret_value()) < 32:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Set JARVIS_MODEL_HMAC_KEY (>=32 chars) before adding or restoring "
                "additional users — a derived key is unsafe on a multi-user deployment."
            ),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    response_model=list[UserRecord],
    dependencies=[Depends(require_admin)],
)
async def list_users(request: Request, include_deleted: bool = False) -> list[UserRecord]:
    """Return all non-deleted users ordered by creation date.

    With ``include_deleted=true`` also returns soft-deleted users still inside
    the 30-day restore grace, so the admin UI can offer a restore action.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        owner = await resolve_owner_identity(conn)
        if include_deleted:
            rows = await conn.fetch(
                """
                SELECT id, email, role, created_at, last_login_at, deleted_at
                FROM users
                WHERE deleted_at IS NULL
                   OR deleted_at >= NOW() - INTERVAL '30 days'
                ORDER BY created_at ASC
                """,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, email, role, created_at, last_login_at
                FROM users
                WHERE deleted_at IS NULL
                ORDER BY created_at ASC
                """,
            )
    return [_row_to_user(row, owner) for row in rows]


@router.post(
    "/owner/transfer",
    response_model=OwnerIdentityResponse,
    dependencies=[Depends(require_admin)],
)
async def transfer_owner(
    body: TransferOwnerBody,
    request: Request,
) -> OwnerIdentityResponse:
    """Transfer database-managed ownership to another live administrator."""
    caller_id: int | None = getattr(request.state, "user_id", None)
    if caller_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin session required")

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('admin_role_mutation'))")
            current = await resolve_owner_identity(conn)

            if current.source == "environment":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Ownership is managed by OWNER_USER_ID; change it on the host "
                        "and restart JARVIS"
                    ),
                )
            if not current.is_valid:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "The stored owner is missing or invalid; repair it with "
                        "jarvis-research owner set <admin-email>"
                    ),
                )
            if current.user_id != caller_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only the current instance owner can transfer ownership",
                )
            if body.target_user_id == caller_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Choose a different administrator as the new owner",
                )

            target = await conn.fetchrow(
                "SELECT id, email, role, deleted_at FROM users WHERE id = $1",
                body.target_user_id,
            )
            if target is None or target["deleted_at"] is not None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Target user not found or deleted",
                )
            if target["role"] != "admin":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="The new owner must already be an administrator",
                )
            if str(body.confirmation) != target["email"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Confirmation must exactly match the target administrator's email",
                )

            updated = await conn.fetchval(
                """
                UPDATE user_config
                SET value = to_jsonb($1::bigint), updated_at = NOW()
                WHERE user_id IS NULL AND key = $2
                RETURNING value #>> '{}'
                """,
                body.target_user_id,
                OWNER_USER_ID_CONFIG_KEY,
            )
            if updated is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "The stored owner changed while the transfer was in progress; "
                        "run jarvis-research owner status and retry"
                    ),
                )

            await log_audit_strict(
                conn,
                action="admin.owner.transfer",
                resource=OWNER_USER_ID_CONFIG_KEY,
                user_id=str(caller_id),
                metadata={
                    "previous_owner_user_id": current.user_id,
                    "new_owner_user_id": body.target_user_id,
                },
            )

    return OwnerIdentityResponse(
        source="database",
        state="valid",
        user_id=body.target_user_id,
    )


@router.post(
    "/users",
    response_model=InviteUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def invite_user(body: InviteUserBody, request: Request) -> InviteUserResponse:
    """Create a new user and send them a 24-hour invite magic link.

    Raises 409 if a non-deleted user with the same email already exists. When
    SMTP cannot deliver the link (unconfigured relay or a send failure), the
    link is returned in ``invite_link`` so the admin can share it manually.
    """
    pool = request.app.state.db_pool
    email_norm = body.email.lower().strip()

    # Compute token values before entering the transaction (no DB I/O needed).
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + INVITE_TOKEN_TTL

    async with pool.acquire() as conn:
        async with conn.transaction():
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

            # Adding a user crosses the box into multi-user — refuse without a real
            # Pulse model-signing key, before the row is created.
            _require_model_hmac_key()

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

            await conn.execute(
                """
                INSERT INTO magic_link_tokens (token_hash, user_id, expires_at)
                VALUES ($1, $2, $3)
                """,
                token_hash,
                int(user_row["id"]),
                expires_at,
            )

    user_id = int(user_row["id"])
    link = _build_invite_link(request, raw_token)
    delivered = False
    try:
        result = await send_magic_link(email_norm, link, pool=pool)
        delivered = result is MagicLinkDelivery.DELIVERED
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

    # Surface the link to the (admin-only) caller only when it could not be
    # delivered, so they can hand it over manually.
    return InviteUserResponse(
        **_row_to_user(user_row).model_dump(),
        invite_link=None if delivered else link,
    )


@router.patch(
    "/users/{user_id}/role",
    response_model=UserRecord,
    dependencies=[Depends(require_admin)],
)
async def update_user_role(user_id: int, body: UpdateRoleBody, request: Request) -> UserRecord:
    """Change a user's role.

    Raises 400 if the change would demote the last remaining admin — for a
    self-demotion or for demoting any other final admin — which would lock
    out every admin.
    Raises 404 if the user does not exist or is soft-deleted.

    The last-admin check and the UPDATE run inside one transaction serialised
    by ``pg_advisory_xact_lock`` so two concurrent demotions cannot both pass
    the count check and leave the box with zero admins.
    """
    pool = request.app.state.db_pool
    caller_id: int | None = getattr(request.state, "user_id", None)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # The advisory lock MUST be acquired inside the open transaction:
            # pg_advisory_xact_lock is released at commit/rollback, so a lock
            # taken in asyncpg autocommit would release at once and serialise
            # nothing. Concurrent admin-role mutations now queue on this lock.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('admin_role_mutation'))")

            old_role: str | None = await conn.fetchval(
                "SELECT role FROM users WHERE id = $1 AND deleted_at IS NULL",
                user_id,
            )
            if old_role is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

            owner = await resolve_owner_identity(conn)
            if (
                old_role == "admin"
                and body.role != "admin"
                and _owner_change_blocked(owner, user_id)
            ):
                detail = (
                    "Change OWNER_USER_ID on the host before demoting the instance owner"
                    if owner.source == "environment"
                    else "Transfer ownership before demoting the instance owner"
                )
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
            if (
                old_role != "admin"
                and body.role == "admin"
                and _owner_repair_blocked(owner, user_id)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_owner_repair_detail(owner),
                )

            if old_role == "admin" and body.role != "admin":
                admin_count: int = await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND deleted_at IS NULL",
                )
                if admin_count <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot demote the last admin",
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

            await log_audit_strict(
                conn,
                action="admin.user.role_change",
                resource=f"users/{user_id}",
                user_id=str(caller_id) if caller_id is not None else None,
                metadata={"old_role": old_role, "new_role": body.role},
            )

    return _row_to_user(row, owner)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    response_model=None,
)
async def soft_delete_user(user_id: int, request: Request, response: Response) -> Response:
    """Soft-delete a user by setting ``deleted_at``.

    Raises 400 if the caller tries to delete themselves, or if deleting the
    target would remove the last remaining admin (locking out every admin).
    Raises 404 if the user does not exist or is already deleted.

    The last-admin check and the UPDATE run inside one transaction serialised
    by ``pg_advisory_xact_lock`` so a concurrent demote/delete cannot race past
    the count check and leave the box with zero admins.
    """
    caller_id: int | None = getattr(request.state, "user_id", None)

    if caller_id is not None and caller_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The advisory lock MUST be acquired inside the open transaction:
            # pg_advisory_xact_lock is released at commit/rollback, so a lock
            # taken in asyncpg autocommit would release at once and serialise
            # nothing. Concurrent admin-role mutations now queue on this lock.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('admin_role_mutation'))")

            target_role: str | None = await conn.fetchval(
                "SELECT role FROM users WHERE id = $1 AND deleted_at IS NULL",
                user_id,
            )
            if target_role is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

            owner = await resolve_owner_identity(conn)
            if _owner_change_blocked(owner, user_id):
                detail = (
                    "Change OWNER_USER_ID on the host before deleting the instance owner"
                    if owner.source == "environment"
                    else "Transfer ownership before deleting the instance owner"
                )
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

            if target_role == "admin":
                admin_count: int = await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND deleted_at IS NULL",
                )
                if admin_count <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot delete the last admin",
                    )

            await conn.execute(
                """
                UPDATE users
                SET deleted_at = NOW()
                WHERE id = $1 AND deleted_at IS NULL
                """,
                user_id,
            )

            # Soft delete only — the daily data_purge job hard-deletes after
            # the 30-day grace, when ownership is no longer attached to this row.
            await log_audit_strict(
                conn,
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

    # Restoring a user can cross the box back into multi-user — refuse without a
    # real Pulse model-signing key, before clearing deleted_at.
    _require_model_hmac_key()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('admin_role_mutation'))")
            target = await conn.fetchrow(
                """
                SELECT id, email, role, created_at, last_login_at, deleted_at
                FROM users
                WHERE id = $1
                  AND deleted_at IS NOT NULL
                  AND deleted_at >= NOW() - INTERVAL '30 days'
                """,
                user_id,
            )
            if target is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found, not deleted, or past the 30-day restore grace",
                )

            owner = await resolve_owner_identity(conn)
            if target["role"] == "admin" and _owner_repair_blocked(owner, user_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_owner_repair_detail(owner),
                )

            row = await conn.fetchrow(
                """
                UPDATE users
                SET deleted_at = NULL
                WHERE id = $1 AND deleted_at IS NOT NULL
                RETURNING id, email, role, created_at, last_login_at
                """,
                user_id,
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The user changed while restore was in progress; reload and retry",
                )

    await log_audit(
        pool,
        action="admin.user.restore",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
    )

    return _row_to_user(row, owner)


@router.post(
    "/users/{user_id}/send-link",
    response_model=SendLinkResponse,
    dependencies=[Depends(require_admin)],
)
async def send_sign_in_link(user_id: int, request: Request) -> SendLinkResponse:
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
    delivered = False
    try:
        result = await send_magic_link(email, link, pool=pool)
        delivered = result is MagicLinkDelivery.DELIVERED
    except Exception:  # noqa: BLE001 — never expose SMTP errors
        logger.exception("send_magic_link (sign-in) failed for user_id=%s", user_id)

    caller_id = getattr(request.state, "user_id", None)
    await log_audit(
        pool,
        action="admin.user.send_link",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
    )

    return SendLinkResponse(sent=delivered, sent_link=None if delivered else link)


# ---------------------------------------------------------------------------
# Passkey administration
# ---------------------------------------------------------------------------


class PasskeyCountResponse(BaseModel):
    count: int


class PasskeyRevokeAllResponse(BaseModel):
    revoked_credentials: int
    revoked_sessions: int


@router.get(
    "/users/{user_id}/passkeys",
    response_model=PasskeyCountResponse,
    dependencies=[Depends(require_admin)],
)
async def get_user_passkey_count(user_id: int, request: Request) -> PasskeyCountResponse:
    """Return how many passkeys a user has registered (read-only support signal)."""
    pool = request.app.state.db_pool
    count = await pool.fetchval(
        "SELECT count(*) FROM webauthn_credentials WHERE user_id = $1", user_id
    )
    return PasskeyCountResponse(count=int(count or 0))


@router.post(
    "/users/{user_id}/passkeys/revoke-all",
    response_model=PasskeyRevokeAllResponse,
    dependencies=[Depends(require_admin)],
)
async def revoke_user_passkeys(user_id: int, request: Request) -> PasskeyRevokeAllResponse:
    """Delete all of a user's passkeys and revoke the sessions those passkeys minted.

    Recovery channel for a lost/compromised authenticator. Magic-link and api-key
    sessions (``credential_id IS NULL``) are left intact so the admin sign-in-link
    path stays usable. Sessions are revoked BEFORE the credentials are deleted so
    the ``credential_id`` FK link still resolves.
    """
    pool = request.app.state.db_pool
    caller_id = getattr(request.state, "user_id", None)
    revoked_sessions = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            cred_ids = [
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM webauthn_credentials WHERE user_id = $1 FOR UPDATE", user_id
                )
            ]
            if cred_ids:
                result = await conn.execute(
                    "UPDATE sessions SET revoked_at = now() "
                    "WHERE credential_id = ANY($1::uuid[]) AND revoked_at IS NULL",
                    cred_ids,
                )
                revoked_sessions = int(result.split()[-1])
                await conn.execute("DELETE FROM webauthn_credentials WHERE user_id = $1", user_id)

    await log_audit(
        pool,
        action="admin.user.passkey.revoke_all",
        resource=f"users/{user_id}",
        user_id=str(caller_id) if caller_id is not None else None,
        metadata={"revoked_credentials": len(cred_ids), "revoked_sessions": revoked_sessions},
    )
    return PasskeyRevokeAllResponse(
        revoked_credentials=len(cred_ids), revoked_sessions=revoked_sessions
    )


__all__ = ["router"]
