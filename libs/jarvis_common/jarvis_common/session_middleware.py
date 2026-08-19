"""Session-cookie reading middleware for JARVIS browser sessions.

Parses the ``jarvis_session`` cookie, validates the row in
``sessions``, and populates ``request.state.user_id`` (INTEGER), ``user_email``,
``user_role``, and ``session_id`` for downstream authorization.

Non-rejecting by design: missing/invalid cookies leave ``request.state``
unset. Authorization is the responsibility of ``verify_api_key`` (for non-
browser callers) and per-route dependency injection (for browser callers).

Both X-API-Key and session cookies are accepted simultaneously. When both are
present the session cookie takes priority for ``request.state.user_id``
(browsers identify the user; the API key only proves request authenticity).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


@dataclass(frozen=True, slots=True)
class _ResolvedSession:
    """Validated identity fields shared by concurrent requests for one session."""

    user_id: int
    email: str
    role: str
    renewed: bool


_SESSION_LOOKUPS: dict[tuple[int, str], asyncio.Task[_ResolvedSession | None]] = {}


def session_cookie_kwargs(max_age: int, *, now: datetime) -> dict[str, Any]:
    """Return the shared browser-session cookie attributes.

    Parameters
    ----------
    max_age : int
        Cookie lifetime in seconds.
    now : datetime
        Aware UTC timestamp used to derive the absolute expiry.

    Returns
    -------
    dict[str, Any]
        Attributes passed to Starlette's ``set_cookie`` helper. Callers supply
        the cookie name and value separately.

    Notes
    -----
    Both ``max_age`` and the absolute ``expires`` timestamp are emitted so mint
    and rolling-refresh paths serialize equivalent cookie policies.
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
    """Create a browser session and attach its cookie to a response.

    Parameters
    ----------
    conn : Any
        Database connection authorized to call the session-mint capability.
    response : Response
        Response that receives the session cookie.
    user_id : int
        Owner of the new session.
    now : datetime
        Aware UTC creation timestamp.
    credential_id : uuid.UUID or None, optional
        Passkey credential that established the session, when applicable.

    Returns
    -------
    str
        Identifier of the newly created session.

    Notes
    -----
    This is the single mint path for magic-link verification, API-key session
    exchange, first-administrator bootstrap, and passkey login.
    """
    session_id = await conn.fetchval(
        "SELECT platform.mint_session_v1($1, $2, $3)",
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


class SessionMiddleware:
    """Populate ``request.state.user_id`` from the session cookie.

    Best-effort; never raises. DB pool is read from ``request.app.state.db_pool``
    on each call so the middleware can sit in the stack before the lifespan
    has run (during which ``app.state.db_pool`` doesn't exist yet) — in that
    case we degrade silently.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an ASGI application with browser-session resolution.

        Parameters
        ----------
        app : ASGIApp
            Application that receives resolved identity in request state.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Resolve one HTTP session and relay any renewal cookie.

        Parameters
        ----------
        scope : Scope
            ASGI connection scope.
        receive : Receive
            ASGI receive callable.
        send : Send
            ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            await _populate_state_from_cookie(request, token)

        renewal_header: bytes | None = None
        if getattr(request.state, "session_renewed", None):
            renewal = Response()
            renewal.set_cookie(
                SESSION_COOKIE_NAME,
                request.cookies[SESSION_COOKIE_NAME],
                **session_cookie_kwargs(int(SESSION_TTL.total_seconds()), now=datetime.now(UTC)),
            )
            renewal_header = next(
                value for name, value in renewal.raw_headers if name == b"set-cookie"
            )

        session_cookie_prefix = f"{SESSION_COOKIE_NAME}=".encode()

        async def send_with_renewal(message: Message) -> None:
            """Append the renewal cookie without buffering the response body.

            A handler that sets the session cookie itself owns the session for
            this response: sign-out clears it, and sign-in mints a new one.
            Appending a rolling renewal after either would put a live cookie
            last and undo the handler's decision.
            """
            if message["type"] == "http.response.start" and renewal_header is not None:
                headers = list(message.get("headers", []))
                handler_set_session = any(
                    name.lower() == b"set-cookie" and value.startswith(session_cookie_prefix)
                    for name, value in headers
                )
                if not handler_set_session:
                    headers.append((b"set-cookie", renewal_header))
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_renewal)


async def _populate_state_from_cookie(request: Request, session_id: str) -> None:
    """Look up session row and attach user fields to request.state.

    Swallows every error: a missing pool, malformed UUID, expired session, or
    revoked session all result in ``request.state.user_id`` remaining unset.
    """
    pool = getattr(getattr(request.app, "state", None), "db_pool", None)
    if pool is None:
        return
    resolved = await _resolve_session_shared(pool, session_id)
    if resolved is None:
        return
    request.state.user_id = resolved.user_id
    request.state.user_email = resolved.email
    request.state.user_role = resolved.role
    request.state.session_id = session_id
    if resolved.renewed:
        request.state.session_renewed = session_id


async def _resolve_session_shared(pool: Any, session_id: str) -> _ResolvedSession | None:
    """Coalesce only overlapping lookups for the same pool and session.

    Completed work is removed immediately, so revocation and deletion checks are
    never served from a time-based cache. Shielding keeps one disconnected client
    from cancelling the shared database operation for its peers.
    """
    key = (id(pool), session_id)
    task = _SESSION_LOOKUPS.get(key)
    if task is None:
        task = asyncio.create_task(_resolve_session(pool, session_id))
        _SESSION_LOOKUPS[key] = task
        task.add_done_callback(lambda completed: _discard_session_lookup(key, completed))
    return await asyncio.shield(task)


def _discard_session_lookup(
    key: tuple[int, str], completed: asyncio.Task[_ResolvedSession | None]
) -> None:
    """Remove a finished single-flight lookup without disturbing a replacement."""
    if _SESSION_LOOKUPS.get(key) is completed:
        del _SESSION_LOOKUPS[key]


async def _resolve_session(pool: Any, session_id: str) -> _ResolvedSession | None:
    """Resolve and optionally renew one session through one acquired connection."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SESSION_LOOKUP_SQL, session_id)
            if row is None:
                return None
            if row["revoked_at"] is not None:
                return None
            if row["deleted_at"] is not None:
                return None
            # asyncpg returns timezone-aware datetimes for TIMESTAMPTZ columns.
            # Offline-tolerant: a session expired within SESSION_GRACE still resolves
            # identity so reviews queued offline reconcile after a realistic offline gap.
            # Such grace-expired rows are NOT renewed below — the owner capability
            # requires expires_at > now(), so only still-live sessions roll forward.
            expires_at = row["expires_at"]
            if expires_at is None:
                return None
            now = datetime.now(UTC)
            if expires_at <= now - SESSION_GRACE:
                return None
            renewed = False
            if now < expires_at < now + SESSION_TTL - SESSION_RENEW_AFTER:
                renewed = await _renew_session(conn, session_id)
            return _ResolvedSession(
                user_id=int(row["user_id"]),
                email=str(row["email"]),
                role=str(row["role"]),
                renewed=renewed,
            )
    except Exception:  # noqa: BLE001 — middleware must never raise
        # Every signed-in caller degrades to anonymous when this fires, so it is
        # reported at a level an operator sees rather than left to debug logs.
        logger.warning("session lookup failed; requests degrade to anonymous", exc_info=True)
        return None


async def _renew_session(conn: Any, session_id: str) -> bool:
    """Roll an in-use session's expiry forward, at most once per SESSION_RENEW_AFTER.

    The owner capability's predicate is the security boundary: a grace-resolved
    (already-expired) or revoked session returns no row and is never renewed. This
    runs only after identity is resolved, so — like the lookup — it is best-effort:
    a renewal failure must never surface and break the request.
    """
    try:
        renewed = await conn.fetchval(
            "SELECT platform.renew_session_v1($1, $2, $3)",
            session_id,
            SESSION_TTL,
            SESSION_RENEW_AFTER,
        )
    except Exception:  # noqa: BLE001 — renewal is best-effort; request identity already set
        logger.debug("session renewal failed (non-fatal)", exc_info=True)
        return False
    return renewed is not None


__all__ = [
    "SessionMiddleware",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL",
    "SESSION_RENEW_AFTER",
    "session_cookie_kwargs",
    "mint_session",
]
