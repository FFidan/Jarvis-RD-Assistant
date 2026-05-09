"""Session-cookie reading middleware for JARVIS browser sessions.

Phase 2 WS-2A: parses the ``jarvis_session`` cookie, validates the row in
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
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "jarvis_session"


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
"""


class SessionMiddleware(BaseHTTPMiddleware):
    """Populate ``request.state.user_id`` from the session cookie.

    Best-effort; never raises. DB pool is read from ``request.app.state.db_pool``
    on each call so the middleware can sit in the stack before the lifespan
    has run (during which ``app.state.db_pool`` doesn't exist yet) — in that
    case we degrade silently.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            await _populate_state_from_cookie(request, token)
        return await call_next(request)


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
    expires_at = row["expires_at"]
    from datetime import UTC, datetime  # noqa: PLC0415

    if expires_at is not None and expires_at <= datetime.now(UTC):
        return
    request.state.user_id = int(row["user_id"])
    request.state.user_email = row["email"]
    request.state.user_role = row["role"]


__all__ = ["SessionMiddleware", "SESSION_COOKIE_NAME"]
