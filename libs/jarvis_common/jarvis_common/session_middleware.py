"""Session-cookie reading middleware for JARVIS browser sessions.

Parses the ``jarvis_session`` cookie, validates the row in
``sessions``, and populates ``request.state.user_id`` (INTEGER), ``user_email``,
``user_role`` for downstream code (``current_user_id_or_none``, route handlers).

Non-rejecting by design: missing/invalid cookies leave ``request.state``
unset. Authorization is the responsibility of ``verify_api_key`` (for non-
browser callers) and per-route dependency injection (for browser callers).

Both X-API-Key and session cookies are accepted simultaneously. When both are
present the session cookie takes priority for ``request.state.user_id``
(browsers identify the user; the API key only proves request authenticity).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from jarvis_common.settings import get_core_settings

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "jarvis_session"

# Offline-tolerant grace: a session expired by no more than this still resolves
# identity (without renewing expires_at) so reviews taken offline reconcile
# after a realistic offline gap. revoked_at/deleted_at still hard-fail.
SESSION_GRACE = timedelta(hours=24)

# Rolling renewal: an in-use session rolls its expiry forward to SESSION_TTL, but
# at most once per SESSION_RENEW_AFTER so a busy user triggers a single write/day.
SESSION_TTL = timedelta(days=30)
SESSION_RENEW_AFTER = timedelta(days=1)


_SESSION_LOOKUP_SQL = """
SELECT
    s.user_id,
    s.expires_at,
    s.revoked_at,
    u.email,
    u.role,
    u.deleted_at
FROM sessions s
JOIN users u ON u.id = s.user_id
WHERE s.id = $1::uuid
    AND s.expires_at IS NOT NULL
"""

# Atomic rolling renewal. The WHERE clause is the security boundary:
#   revoked_at IS NULL           — never renew a revoked session
#   expires_at > now()           — a grace-resolved (already-expired) session is NON-renewable
#   expires_at < now()+$2-$3     — throttle to at most one write per session per SESSION_RENEW_AFTER
# $2/$3 are cast to ::interval: asyncpg sends timedeltas as untyped params, and
# Postgres otherwise resolves $3 in "now() + $2 - $3" as timestamptz, making the
# right side an interval so "expires_at < interval" fails to prepare (verified on pg16.8).
_SESSION_RENEW_SQL = """
UPDATE sessions SET expires_at = now() + $2::interval
WHERE id = $1::uuid
    AND revoked_at IS NULL
    AND expires_at > now()
    AND expires_at < now() + $2::interval - $3::interval
RETURNING id
"""


def session_cookie_kwargs(max_age: int, *, now: datetime) -> dict[str, Any]:
    """Return the ``jarvis_session`` cookie attributes shared by every mint/refresh site.

    Callers pass ``key``/``value`` positionally; this dict carries the rest. Both
    ``max_age`` and the absolute ``expires`` timestamp are emitted so the serialised
    Set-Cookie header is byte-identical across all sites (mint and rolling refresh).
    """
    return {
        "max_age": max_age,
        # Pass an aware-UTC datetime, NOT an epoch int: starlette's set_cookie
        # feeds a non-datetime expires to http.cookies._getdate, which treats the
        # int as seconds-from-now and serialises an Expires ~60y out. A datetime
        # takes the format_datetime(usegmt=True) branch → correct absolute date.
        "expires": now + timedelta(seconds=max_age),
        "httponly": True,
        "secure": not get_core_settings().dev_mode,
        "samesite": "strict",
        "path": "/",
    }


async def mint_session(
    conn: Any,
    response: Response,
    user_id: int,
    *,
    now: datetime,
    credential_id: uuid.UUID | None = None,
) -> str:
    """Insert a ``sessions`` row and set the ``jarvis_session`` cookie; return its id.

    The single mint path shared by magic-link verify, API-key session exchange,
    first-admin bootstrap, and passkey login. ``credential_id`` links the session
    to the passkey it was minted from (NULL for the passwordless flows). Cookie
    attributes come from :func:`session_cookie_kwargs`, so every mint site emits a
    byte-identical Set-Cookie (both ``max_age`` and the absolute ``expires``).
    """
    session_id = await conn.fetchval(
        "INSERT INTO sessions (user_id, expires_at, credential_id) "
        "VALUES ($1, $2, $3) RETURNING id",
        user_id,
        now + SESSION_TTL,
        credential_id,
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        str(session_id),
        **session_cookie_kwargs(int(SESSION_TTL.total_seconds()), now=now),
    )
    return str(session_id)


class SessionMiddleware(BaseHTTPMiddleware):
    """Populate ``request.state.user_id`` from the session cookie.

    Best-effort; never raises. DB pool is read from ``request.app.state.db_pool``
    on each call so the middleware can sit in the stack before the lifespan
    has run (during which ``app.state.db_pool`` doesn't exist yet) — in that
    case we degrade silently.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Pass *app* to ``BaseHTTPMiddleware``; no additional state is needed."""
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        """Populate ``request.state`` from the session cookie, refreshing it when renewed."""
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            await _populate_state_from_cookie(request, token)
        response = await call_next(request)
        if getattr(request.state, "session_renewed", None):
            response.set_cookie(
                SESSION_COOKIE_NAME,
                request.cookies[SESSION_COOKIE_NAME],
                **session_cookie_kwargs(int(SESSION_TTL.total_seconds()), now=datetime.now(UTC)),
            )
        return response


async def _populate_state_from_cookie(request: Request, session_id: str) -> None:
    """Look up session row and attach user fields to request.state.

    Swallows every error: a missing pool, malformed UUID, expired session, or
    revoked session all result in ``request.state.user_id`` remaining unset.
    """
    pool = getattr(getattr(request.app, "state", None), "db_pool", None)
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SESSION_LOOKUP_SQL, session_id)
    except Exception:  # noqa: BLE001 — middleware must never raise
        logger.debug("session lookup failed (non-fatal)", exc_info=True)
        return
    if row is None:
        return
    if row["revoked_at"] is not None:
        return
    if row["deleted_at"] is not None:
        return
    # asyncpg returns timezone-aware datetimes for TIMESTAMPTZ columns.
    # Offline-tolerant: a session expired within SESSION_GRACE still resolves
    # identity so reviews queued offline reconcile after a realistic offline gap.
    # Such grace-expired rows are NOT renewed below — _SESSION_RENEW_SQL requires
    # expires_at > now(), so only still-live sessions roll their expiry forward.
    expires_at = row["expires_at"]
    if expires_at is None:
        return
    if expires_at <= datetime.now(UTC) - SESSION_GRACE:
        return
    request.state.user_id = int(row["user_id"])
    request.state.user_email = row["email"]
    request.state.user_role = row["role"]
    await _renew_session(request, pool, session_id)


async def _renew_session(request: Request, pool: Any, session_id: str) -> None:
    """Roll an in-use session's expiry forward, at most once per SESSION_RENEW_AFTER.

    ``_SESSION_RENEW_SQL``'s predicate is the security boundary: a grace-resolved
    (already-expired) or revoked session returns no row and is never renewed. This
    runs only after identity is resolved, so — like the lookup — it is best-effort:
    a renewal failure must never surface and break the request.
    """
    try:
        async with pool.acquire() as conn:
            renewed = await conn.fetchval(
                _SESSION_RENEW_SQL, session_id, SESSION_TTL, SESSION_RENEW_AFTER
            )
    except Exception:  # noqa: BLE001 — renewal is best-effort; request identity already set
        logger.debug("session renewal failed (non-fatal)", exc_info=True)
        return
    if renewed is not None:
        request.state.session_renewed = session_id


__all__ = [
    "SessionMiddleware",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL",
    "SESSION_RENEW_AFTER",
    "session_cookie_kwargs",
    "mint_session",
]
