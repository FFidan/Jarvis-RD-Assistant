"""Account — self-service current-user profile endpoints.

Three endpoints, all strictly scoped to the authenticated caller via
``current_user_id_strict`` (never another user's row):

- ``GET  /api/account``                — read own profile.
- ``PATCH /api/account``               — update ``display_name`` immediately;
  an ``email`` change is **never silent** — it issues a single-use,
  15-minute, SHA-256-hashed token to the *new* address and swaps
  ``users.email`` only when that token is confirmed.
- ``POST /api/account/confirm-email``  — consume the email-change token
  (mirrors ``/api/auth/verify``'s atomic, replay-safe consume logic).

Admin user-management lives in ``platform_api.routers.admin``
(``/api/admin/*``) and is intentionally untouched. Crypto helpers
(``_hash_token``, ``_hash_email``, ``_audit_pool``) are imported from
``auth`` — no duplication.

Note: ``from __future__ import annotations`` is intentionally absent — see
``routers/my_day.py`` / ``docs/plans/2026-04-29-future-import-failure-analysis.md``
for the verified PydanticUserError trace. ``AccountUpdate`` /
``ConfirmEmailChangeBody`` are FastAPI request-body models; PEP-563
stringized annotations make FastAPI misclassify them as query params and
break ``app.openapi()``. Body annotations must remain concrete types.
"""

import logging
import secrets
from datetime import UTC, datetime
from urllib.parse import urlencode

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from jarvis_common.audit import log_audit
from jarvis_common.auth import current_user_id_strict
from jarvis_common.email import MagicLinkDelivery, send_magic_link

from platform_api.config import get_platform_settings
from platform_api.deps import limiter
from platform_api.models.account import (
    AccountResponse,
    AccountUpdate,
    AccountUpdateResponse,
    ConfirmEmailChangeBody,
)
from platform_api.routers._auth_shared import magic_link_on_cooldown
from platform_api.routers.auth import (
    MAGIC_LINK_TTL,
    _audit_pool,
    _hash_email,
    _hash_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


def _build_email_confirm_link(request: Request, token: str) -> str:
    """URL the user clicks to confirm an email change.

    Same derivation strategy as ``auth._build_magic_link`` (honours
    ``APP_BASE_URL`` / ProxyHeaders), but lands on the existing account pane.
    The bearer token rides a fragment, which browsers do not send in request
    lines or Referer headers.
    """
    base = get_platform_settings().app_base_url
    query = "section=account&item=profile"
    fragment = urlencode({"confirm_email_token": token})
    if base:
        settings_url = f"{base.rstrip('/')}/settings?{query}"
    else:
        settings_url = str(request.url.replace(path="/settings", query=query))
    return f"{settings_url}#{fragment}"


def _row_to_account(row: asyncpg.Record) -> AccountResponse:
    return AccountResponse(
        id=int(row["id"]),
        email=row["email"],
        role=row["role"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


_ACCOUNT_SELECT = (
    "SELECT id, email, role, display_name, created_at, last_login_at "
    "FROM users WHERE id = $1 AND deleted_at IS NULL"
)


@router.get("", response_model=AccountResponse)
@limiter.limit("60/minute")
async def get_account(
    request: Request,
    user_id: int = Depends(current_user_id_strict),
) -> AccountResponse:
    """Return the authenticated caller's own profile.

    Scoped strictly to ``current_user_id_strict`` — there is no path
    parameter and no way to read another user's row.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ACCOUNT_SELECT, user_id)
    if row is None:
        # Session pointed at a now-deleted user — treat as unauthenticated.
        raise HTTPException(status_code=401, detail="Authentication required")
    return _row_to_account(row)


async def _stage_email_change(
    conn: asyncpg.Connection,
    current_email: str,
    requested_email: str,
    request: Request,
    user_id: int,
) -> tuple[tuple[str, str] | None, tuple[str, dict | None] | None, bool]:
    """Validate and stage a verified email change under an open connection.

    Returns ``(pending_send, audit_event, email_conflict)`` and runs NO
    post-release side-effects (no email sent, no audit written): send_magic_link
    and log_audit each re-acquire from the pool, so the caller must run them only
    after the connection is released (else a saturated pool deadlocks).
    ``email_conflict`` is True when the address is already in use so the caller
    can defer the 409 until after any staged display_name audit has flushed.
    """
    new_email = requested_email.lower().strip()
    if new_email == current_email.lower():
        return None, None, False
    # Reject if the address is already in use by another live user.
    clash = await conn.fetchrow(
        "SELECT id FROM users WHERE email = $1 AND deleted_at IS NULL",
        new_email,
    )
    if clash is not None:
        return None, None, True
    if await magic_link_on_cooldown(conn, user_id, email_change=True):
        return None, None, False
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + MAGIC_LINK_TTL
    await conn.execute(
        """
        INSERT INTO magic_link_tokens
            (token_hash, user_id, expires_at, pending_email)
        VALUES ($1, $2, $3, $4)
        """,
        token_hash,
        user_id,
        expires_at,
        new_email,
    )
    link = _build_email_confirm_link(request, raw_token)
    audit_event = (
        "account.email.change.requested",
        {"new_email_hash": _hash_email(new_email)},
    )
    return (new_email, link), audit_event, False


@router.patch("", response_model=AccountUpdateResponse)
@limiter.limit("20/minute")
async def update_account(
    body: AccountUpdate,
    request: Request,
    user_id: int = Depends(current_user_id_strict),
) -> AccountUpdateResponse:
    """Update the caller's own profile.

    - ``display_name`` is applied immediately (empty string clears it).
    - ``email`` is **verified, never silent**: a single-use 15-minute token
      is issued to the *new* address and ``users.email`` is swapped only on
      ``POST /api/account/confirm-email``. An email already in use by a
      non-deleted user is rejected with 409.
    """
    pool = request.app.state.db_pool
    _audit = _audit_pool(request)
    email_verification_sent = False
    # Side-effects are staged under the connection and executed only AFTER it
    # is released (see the block below the acquire).
    pending_send: tuple[str, str] | None = None
    audit_events: list[tuple[str, dict | None]] = []
    # Deferred so a same-request display_name change still flushes its staged
    # audit event before the email-clash 409 is raised (raising inside the
    # acquire block would exit before the post-block audit loop runs).
    email_conflict = False

    async with pool.acquire() as conn:
        current = await conn.fetchrow(_ACCOUNT_SELECT, user_id)
        if current is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        # 1. display_name — immediate.
        if body.display_name is not None:
            new_name = body.display_name.strip() or None
            await conn.execute(
                "UPDATE users SET display_name = $1 WHERE id = $2 AND deleted_at IS NULL",
                new_name,
                user_id,
            )
            audit_events.append(("account.display_name.update", None))

        # 2. email — verified change only (no direct UPDATE users.email here).
        if body.email is not None:
            pending_send, email_audit, email_conflict = await _stage_email_change(
                conn, current["email"], body.email, request, user_id
            )
            if email_audit is not None:
                audit_events.append(email_audit)

        # Re-read so the response reflects the persisted row (email unchanged
        # until the token is confirmed).
        refreshed = await conn.fetchrow(_ACCOUNT_SELECT, user_id)

    # Side-effects run only AFTER the connection is returned to the pool:
    # send_magic_link and log_audit each RE-ACQUIRE a separate connection from
    # this same pool, so invoking them while still holding `conn` hangs
    # (hold-and-wait deadlock) once the pool is saturated.
    if pending_send is not None:
        new_email, link = pending_send
        try:
            result = await send_magic_link(new_email, link, pool=pool)
            email_verification_sent = result is MagicLinkDelivery.DELIVERED
        except Exception:  # noqa: BLE001 — never leak SMTP detail
            logger.exception(
                "send_magic_link (email-change) failed for email_hash=%s",
                _hash_email(new_email),
            )
    if _audit is not None:
        for action, metadata in audit_events:
            await log_audit(
                _audit,
                action=action,
                resource=f"users/{user_id}",
                user_id=str(user_id),
                metadata=metadata,
            )

    if email_conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists",
        )

    return AccountUpdateResponse(
        account=_row_to_account(refreshed),
        email_verification_sent=email_verification_sent,
    )


@router.post("/confirm-email", response_model=AccountResponse)
@limiter.limit("10/minute")
async def confirm_email_change(
    body: ConfirmEmailChangeBody,
    request: Request,
    user_id: int = Depends(current_user_id_strict),
) -> AccountResponse:
    """Consume an email-change token and swap ``users.email``.

    Mirrors ``/api/auth/verify``: the token row is selected ``FOR UPDATE``
    inside a transaction, validated (exists / unused / unexpired / has a
    ``pending_email``), marked ``used_at``, then the email is swapped — all
    atomically so a token cannot be replayed if the UPDATE fails. The caller
    must own the token's ``user_id`` (defence in depth on top of the
    unguessable token).
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
                if (
                    token_row is None
                    or token_row["used_at"] is not None
                    or token_row["expires_at"] <= now
                    or token_row["pending_email"] is None
                    or int(token_row["user_id"]) != user_id
                ):
                    raise HTTPException(status_code=400, detail="Invalid or expired token")

                new_email = token_row["pending_email"].lower().strip()

                # Re-check uniqueness at confirm time (could have been taken
                # between request and confirm).
                clash = await conn.fetchrow(
                    "SELECT id FROM users WHERE email = $1 AND deleted_at IS NULL AND id <> $2",
                    new_email,
                    user_id,
                )
                if clash is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A user with that email already exists",
                    )

                await conn.execute(
                    "UPDATE magic_link_tokens SET used_at = NOW() WHERE token_hash = $1",
                    token_hash,
                )
                try:
                    await conn.execute(
                        "UPDATE users SET email = $1 WHERE id = $2 AND deleted_at IS NULL",
                        new_email,
                        user_id,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A user with that email already exists",
                    ) from exc
                refreshed = await conn.fetchrow(_ACCOUNT_SELECT, user_id)
    except HTTPException:
        if _audit is not None:
            await log_audit(
                _audit,
                action="account.email.change.failure",
                resource=f"users/{user_id}",
                user_id=str(user_id),
            )
        raise

    if refreshed is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    if _audit is not None:
        await log_audit(
            _audit,
            action="account.email.change.success",
            resource=f"users/{user_id}",
            user_id=str(user_id),
        )

    return _row_to_account(refreshed)


__all__ = ["router"]
