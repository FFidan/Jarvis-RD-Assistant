"""API key authentication shared across JARVIS services."""

import hmac
import logging
import os

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from jarvis_common.event_log import log_event
from jarvis_common.secrets import read_secret

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_HEALTH_PATHS = frozenset({"/health", "/health/", "/healthz", "/health/readiness"})


def _load_api_key() -> str | None:
    """Resolve JARVIS_API_KEY once at import time.

    Honours the _FILE convention (JARVIS_API_KEY_FILE) via read_secret().
    Returns None when no key is configured so callers can use a simple truth
    check rather than comparing against an empty string.
    """
    value = read_secret("JARVIS_API_KEY")
    return value if value else None


# Resolved once at import time; avoids a file-read per request.
_CACHED_API_KEY: str | None = _load_api_key()


def refresh_api_key_cache() -> None:
    """Re-resolve the API key from env/file and update the module-level cache.

    Tests that monkeypatch JARVIS_API_KEY after import must call this so the
    cached value reflects the new environment.
    """
    global _CACHED_API_KEY
    _CACHED_API_KEY = _load_api_key()


async def verify_api_key(request: Request, api_key: str | None = Depends(_api_key_header)) -> None:
    """Validate API key.

    SECURITY: DEV_MODE only bypasses auth when JARVIS_API_KEY is *not set*.
    If a key is configured, it is always enforced — even in DEV_MODE.
    Uses the module-level cached key (_CACHED_API_KEY) to avoid re-reading
    the secret on every request.
    """
    jarvis_api_key = _CACHED_API_KEY
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    if request.url.path in _HEALTH_PATHS:
        return
    # /infra-events authenticates via X-Infra-Key (separate secret from
    # JARVIS_API_KEY) so the Vector sidecar doesn't need the main API key.
    # The endpoint enforces its own auth via _check_auth().
    if request.url.path.startswith("/infra-events"):
        return
    # /api/auth/* IS the auth bootstrap surface — magic-link request, magic-link
    # verify, and logout. They cannot themselves require API-key auth without
    # locking out brand-new users who haven't been issued a key yet.
    # WS-2A: these endpoints have their own validation (token TTL + single-use).
    if request.url.path.startswith("/api/auth/"):
        return
    # If a real key is configured, always enforce it (even in DEV_MODE)
    if jarvis_api_key:
        if not hmac.compare_digest(api_key or "", jarvis_api_key):
            # Emit an auth-failure event; failures indicate a potential probe or
            # misconfigured client. Successes are NOT logged (too noisy per-request).
            try:
                _pool = getattr(getattr(request, "app", None), "state", None)
                _pool = getattr(_pool, "db_pool", None) if _pool is not None else None
                if _pool is not None:
                    await log_event(
                        pool=_pool,
                        level="warning",
                        category="auth",
                        source="verify_api_key",
                        message="invalid_api_key",
                        context={
                            "ip": request.client.host if request.client else None,
                        },
                    )
            except Exception:  # noqa: BLE001
                logger.debug("auth event log_event failed (non-fatal)", exc_info=True)
            raise HTTPException(status_code=403, detail="Invalid or missing API key")
        return
    # No key configured — fall back to DEV_MODE check
    if dev_mode:
        logger.warning(
            "DEV_MODE=true AND no JARVIS_API_KEY set — ALL authentication "
            "bypassed on %s. DO NOT USE IN PRODUCTION.",
            request.url.path,
        )
        return
    raise HTTPException(
        status_code=401,
        detail="API key not configured. Set JARVIS_API_KEY or enable DEV_MODE.",
    )


async def require_admin(request: Request) -> None:
    """FastAPI dependency — raise 403 for non-admin browser sessions.

    Reads ``request.state.user_role`` set by
    :class:`jarvis_common.session_middleware.SessionMiddleware` when a valid
    session cookie is present.

    Design: when no session cookie is present (API-key-only callers such as the
    Telegram bot, cron jobs, or DEV_MODE single-tenant) ``user_role`` is absent.
    Those callers are allowed through so the legacy single-tenant path continues
    to work without role infra.  Only browser sessions with an explicit
    ``role != 'admin'`` are rejected with 403.

    Raises
    ------
    fastapi.HTTPException
        403 if the caller has a browser session with a non-admin role.
    """
    role = getattr(request.state, "user_role", None)
    if role is not None and role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


async def current_user_id(request: Request) -> int | None:
    """Return the authenticated user's integer ID, or None.

    Reads from ``request.state.user_id``, populated by
    :class:`jarvis_common.session_middleware.SessionMiddleware` when a valid
    ``jarvis_session`` cookie is present. Falls back to None for callers
    without a browser session (Telegram bot using only ``X-API-Key``,
    health checks, etc.).

    Phase 2 WS-2A replaced the previous single-tenant stub.
    """
    return getattr(request.state, "user_id", None)


async def current_user_id_or_none(request: Request) -> int | None:
    """Explicit-intent alias for :func:`current_user_id`.

    Prefer this name in ``Depends(...)`` injection points so the call-site
    reads "I know this can be None and I handle it." Same body as
    :func:`current_user_id` — both read ``request.state.user_id`` set by
    the session middleware.
    """
    return getattr(request.state, "user_id", None)


def assert_multi_tenant_not_implemented() -> None:
    """Raise ``NotImplementedError`` to guard code paths requiring real user IDs.

    Retained for callers that hard-block on a real identity. With WS-2A live
    this is now reachable only when no session is present AND the caller
    explicitly invoked the guard — so it doubles as a "not authenticated"
    signal.

    Raises
    ------
    NotImplementedError
        When called from a code path with no resolved user identity.
    """
    raise NotImplementedError(
        "no authenticated user available; route requires a session or owner identity"
    )


def single_tenant_user_id() -> None:  # type: ignore[return]
    """Legacy alias retained for the Telegram bot.

    The Telegram bot has no browser session and its callers historically passed
    ``None`` as ``user_id``. Returning ``None`` keeps the legacy single-tenant
    Telegram path working until WS-2D rewires Telegram to the per-chat user
    pairing table.

    Returns
    -------
    None
        Sentinel for "no resolved per-user identity" — DB queries match this
        via ``IS NOT DISTINCT FROM NULL``.
    """
    return None  # allow-user-id-none: legacy Telegram single-tenant path


def validate_production_config() -> None:
    """Crash at startup if production config is unsafe.

    Raises
    ------
    RuntimeError
        If ENVIRONMENT=production AND DEV_MODE=true, or if not in DEV_MODE
        and JARVIS_API_KEY is empty, a default sentinel, or shorter than 32 chars.
    """
    env = os.environ.get("ENVIRONMENT", "").lower()
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    api_key = read_secret("JARVIS_API_KEY")

    if env == "production" and dev_mode:
        raise RuntimeError("DEV_MODE=true is not allowed in ENVIRONMENT=production")

    if not dev_mode:
        if not api_key or api_key == "CHANGE_ME_REQUIRED":
            raise RuntimeError(
                "JARVIS_API_KEY must be set to a real value (not empty or default sentinel)"
            )
        if len(api_key) < 32:
            raise RuntimeError(
                f"JARVIS_API_KEY must be at least 32 characters (got {len(api_key)})"
            )
