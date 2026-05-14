"""API key authentication shared across JARVIS services."""

import hmac
import ipaddress
import logging

import asyncpg
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from jarvis_common.event_log import log_event
from jarvis_common.settings import get_core_settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_HEALTH_PATHS = frozenset({"/health", "/health/", "/healthz", "/health/readiness"})


def _load_api_key() -> str | None:
    """Resolve JARVIS_API_KEY once at import time (and on explicit refresh).

    Constructs a fresh SecretsSettings snapshot so that callers of
    refresh_api_key_cache() — e.g. tests that monkeypatch JARVIS_API_KEY —
    always see the current env rather than a stale lru_cache'd result.
    Returns None when no key is configured.
    """
    from jarvis_common.settings import SecretsSettings  # noqa: PLC0415

    value = SecretsSettings().jarvis_api_key
    return value.get_secret_value() if value is not None else None


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
    dev_mode = get_core_settings().dev_mode
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


def _resolve_request_user_id(request: Request) -> int | None:
    """Best-effort extraction of ``request.state.user_id`` as an ``int``.

    Returns ``None`` when:
    - the request object lacks ``.state`` (e.g. ``SimpleNamespace`` test mocks),
    - ``state`` lacks ``user_id`` (no session middleware ran),
    - the attribute is not coercible to an ``int`` (e.g. ``MagicMock``
      auto-attributes in tests).

    Production session middleware always sets an ``int`` here, so the strict
    ``int`` check only filters out test-double noise; it never drops a real
    authenticated identity.
    """
    state = getattr(request, "state", None)
    if state is None:
        return None
    user_id = getattr(state, "user_id", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return None
    return user_id


async def current_user_id(request: Request) -> int | None:
    """Return the authenticated user's integer ID, or None.

    Reads from ``request.state.user_id``, populated by
    :class:`jarvis_common.session_middleware.SessionMiddleware` when a valid
    ``jarvis_session`` cookie is present. Falls back to None for callers
    without a browser session (Telegram bot using only ``X-API-Key``,
    health checks, etc.).

    Phase 2 WS-2A replaced the previous single-tenant stub. Phase 2 final
    integration hardened the resolver to ignore non-int values so
    ``SimpleNamespace`` / ``MagicMock`` request stand-ins in legacy
    single-tenant unit tests still see ``None``.
    """
    return _resolve_request_user_id(request)


async def current_user_id_or_none(request: Request) -> int | None:
    """Explicit-intent alias for :func:`current_user_id`.

    Prefer this name in ``Depends(...)`` injection points so the call-site
    reads "I know this can be None and I handle it." Same body as
    :func:`current_user_id` — both read ``request.state.user_id`` set by
    the session middleware (with defensive fallback to ``None``).
    """
    return _resolve_request_user_id(request)


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


# ---------------------------------------------------------------------------
# X-Owner-User-Id override — Sprint A (Telegram per-user orchestration)
# ---------------------------------------------------------------------------

_OWNER_OVERRIDE_HEADER = "X-Owner-User-Id"


def _parse_allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse ``OWNER_OVERRIDE_ALLOWED_CIDRS`` env var into network objects.

    Falls back to the default loopback + docker-bridge CIDR list when the
    variable is unset or empty.
    """
    raw = get_core_settings().owner_override_allowed_cidrs
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("OWNER_OVERRIDE_ALLOWED_CIDRS: invalid CIDR %r — skipping", part)
    return networks


def _ip_in_allowlist(ip_str: str | None) -> bool:
    """Return True when *ip_str* falls within one of the allowed CIDRs."""
    if not ip_str:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for net in _parse_allowed_networks():
        if addr in net:
            return True
    return False


async def current_user_id_with_owner_override(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
) -> int | None:
    """Resolve the effective user ID for Telegram-bot orchestrator calls.

    Priority order:
    1. ``request.state.user_id`` set by session middleware (browser session).
    2. ``X-Owner-User-Id`` header — trusted **only** when ALL three guards pass:
       a. The request bears a valid ``JARVIS_API_KEY`` (same check as
          :func:`verify_api_key`).
       b. The source IP is within the allowlist (loopback + docker-bridge by
          default; configurable via ``OWNER_OVERRIDE_ALLOWED_CIDRS``).
       c. The supplied ``user_id`` value is an integer that exists in the
          ``users`` table.

    Returns ``None`` when no identity can be resolved (caller may be an
    unauthenticated health-check or a bot call without a pairing).

    Raises ``HTTPException(403)`` when the header is present but any of the
    three guards fails — this surfaces a misconfiguration loudly rather than
    silently falling back to ``None``.
    """
    # 1. Session-authenticated caller wins.
    uid = _resolve_request_user_id(request)
    if uid is not None:
        return uid

    # 2. X-Owner-User-Id override path.
    raw_override = request.headers.get(_OWNER_OVERRIDE_HEADER)
    if raw_override is None:
        return None

    # Guard (a): valid API key required.
    jarvis_api_key = _CACHED_API_KEY
    if not jarvis_api_key or not hmac.compare_digest(api_key or "", jarvis_api_key):
        logger.warning(
            "X-Owner-User-Id header present but API key check failed from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=403,
            detail="X-Owner-User-Id requires a valid X-API-Key",
        )

    # Guard (b): source IP must be in the allowlist.
    client_ip = request.client.host if request.client else None
    if not _ip_in_allowlist(client_ip):
        logger.warning(
            "X-Owner-User-Id header rejected: IP %s not in allowlist",
            client_ip,
        )
        raise HTTPException(
            status_code=403,
            detail="X-Owner-User-Id not allowed from this source IP",
        )

    # Parse the user_id value.
    try:
        override_uid = int(raw_override)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=403,
            detail="X-Owner-User-Id must be an integer",
        ) from None

    # Guard (c): user_id must exist in the users table.
    # We access the DB pool via app.state — same pattern as other auth helpers.
    try:
        pool: asyncpg.Pool | None = getattr(getattr(request, "app", None), "state", None)
        pool = getattr(pool, "db_pool", None) if pool is not None else None
        if pool is None:
            raise HTTPException(
                status_code=503,
                detail="DB pool unavailable for X-Owner-User-Id validation",
            )
        exists = await pool.fetchval(
            "SELECT 1 FROM users WHERE id = $1 AND deleted_at IS NULL",
            override_uid,
        )
        if not exists:
            logger.warning(
                "X-Owner-User-Id user_id=%d does not exist or is deleted",
                override_uid,
            )
            raise HTTPException(
                status_code=403,
                detail="X-Owner-User-Id references unknown user",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("DB error during X-Owner-User-Id validation")
        raise HTTPException(
            status_code=503,
            detail="DB error validating X-Owner-User-Id",
        ) from None

    return override_uid


# allow-user-id-none: legacy Telegram single-tenant path


def validate_production_config() -> None:
    """Crash at startup if production config is unsafe."""
    from jarvis_common.settings import get_core_settings, get_secrets_settings  # noqa: PLC0415

    core = get_core_settings()
    env = core.environment.lower()
    dev_mode = core.dev_mode
    api_key_secret = get_secrets_settings().jarvis_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret else ""

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

    # H14 — Pulse model HMAC key gate. The pulse classifier signs pickle blobs
    # with HMAC-SHA256; without a real key, an attacker with DB write access
    # could forge a signed blob and trigger RCE via pickle.loads. The dedicated
    # JARVIS_MODEL_HMAC_KEY is preferred to avoid dual-use compromise with the
    # HTTP bearer; otherwise the key is derived from JARVIS_API_KEY with a
    # domain-separated SHA-256. In production, refuse to start unless at least
    # one path is configured.
    if env == "production":
        import os as _os  # noqa: PLC0415

        model_hmac_key = _os.environ.get("JARVIS_MODEL_HMAC_KEY", "")
        if not model_hmac_key and not api_key:
            raise RuntimeError(
                "Pulse model HMAC key required in production: set "
                "JARVIS_MODEL_HMAC_KEY (preferred) or JARVIS_API_KEY. "
                "See docs/SECURITY.md#pulse-model-signing."
            )
