"""API key authentication shared across JARVIS services."""

import hmac
import ipaddress
import logging

import asyncpg
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from jarvis_common.audit import log_audit
from jarvis_common.event_log import log_event
from jarvis_common.settings import get_core_settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_HEALTH_PATHS = frozenset({"/health", "/health/", "/health/live", "/healthz", "/health/readiness"})

# Production secret-strength gate (SEC-A). Minimum lengths mirror the project
# convention enforced by scripts/production-readiness-check.sh so the boot gate
# and the readiness script agree.
_LITELLM_MASTER_KEY_MIN_LEN = 16
_POSTGRES_PASSWORD_MIN_LEN = 16
_POSTGRES_PASSWORD_SECRET_PATH = "/run/secrets/postgres_password"

# Known placeholder / known-weak secret values rejected in production. This is a
# verbatim port of the `_is_weak_secret` shell helper in
# scripts/production-readiness-check.sh — keep the two in sync.
_WEAK_SECRET_EXACT = frozenset(
    {
        "",
        "changeme",
        "password",
        "secret",
        "test",
        "dev",
        "jarvis_dev",
        "sk-jarvis-dev-test",
        "sk-1234",
        "1234",
        "admin",
        "postgres",
    }
)
_WEAK_SECRET_SUBSTRINGS = (
    "changeme",
    "placeholder",
    "example",
    "default",
    "replace_me",
    "your_",
    "<",
    "fixme",
)


def _is_weak_secret(value: str) -> bool:
    """True if ``value`` is empty or a known placeholder/skeleton secret.

    Mirrors ``_is_weak_secret`` in scripts/production-readiness-check.sh: an
    exact (case-sensitive) match against a small denylist, plus a
    case-insensitive substring scan for common skeleton fragments.
    """
    if value in _WEAK_SECRET_EXACT:
        return True
    lowered = value.lower()
    return any(fragment in lowered for fragment in _WEAK_SECRET_SUBSTRINGS)


def _request_db_pool(request: Request) -> asyncpg.Pool | None:
    """Best-effort ``app.state.db_pool`` lookup tolerant of test mocks."""
    state = getattr(getattr(request, "app", None), "state", None)
    return getattr(state, "db_pool", None) if state is not None else None


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


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
    core = get_core_settings()
    if request.url.path in _HEALTH_PATHS:
        return
    # /infra-events authenticates via X-Infra-Key (separate secret from
    # JARVIS_API_KEY) so the Vector sidecar doesn't need the main API key.
    # The endpoint enforces its own auth via _check_auth().
    if request.url.path == "/infra-events" or request.url.path.startswith("/infra-events/"):
        return
    # /api/auth/* IS the auth bootstrap surface — magic-link request, magic-link
    # verify, and logout. They cannot themselves require API-key auth without
    # locking out brand-new users who haven't been issued a key yet.
    # WS-2A: these endpoints have their own validation (token TTL + single-use).
    if request.url.path.startswith("/api/auth/"):
        return
    # /api/setup/* IS the first-run bootstrap surface — the FirstRunGate polls
    # /api/setup/status on every boot with no credentials in hand, and the
    # wizard's create-first-admin / system-check / configure-smtp routes run
    # before any session or API-key is established in the browser. Route-level
    # `require_unconfigured_or_admin` is the real gate: open until an admin
    # exists, admin-only after. Without this exemption, FirstRunGate 403s and
    # the whole UI hangs on the loading spinner.
    if request.url.path.startswith("/api/setup/"):
        return
    # WS-AUTH-KEY-SESSION: a valid browser session is sufficient to pass this
    # global front-door gate. SessionMiddleware (ASGI middleware, runs BEFORE
    # router dependencies) sets request.state.user_id (int) only for a
    # non-revoked, non-expired session whose user is not deleted; an
    # expired/revoked/deleted-user session leaves it unset and falls through to
    # the X-API-Key/403 path below. This gate only decides "allowed past the
    # front door"; identity/authz is still enforced per-route downstream by
    # current_user_id_strict / require_admin (which read role/identity
    # independently — a session passing here confers no ops/admin rights).
    if getattr(getattr(request, "state", None), "user_id", None) is not None:
        return
    # If a real key is configured, always enforce it (even in DEV_MODE)
    if jarvis_api_key:
        if not hmac.compare_digest(api_key or "", jarvis_api_key):
            # Emit an auth-failure event; failures indicate a potential probe or
            # misconfigured client. Successes are NOT logged (too noisy per-request).
            try:
                _pool = _request_db_pool(request)
                if _pool is not None:
                    _ip = _client_ip(request)
                    await log_event(
                        pool=_pool,
                        level="warning",
                        category="auth",
                        source="verify_api_key",
                        message="invalid_api_key",
                        context={"ip": _ip},
                    )
                    await log_audit(
                        _pool,
                        action="auth.api_key.invalid",
                        resource=request.url.path,
                        metadata={"ip": _ip},
                    )
            except Exception:  # noqa: BLE001
                logger.debug("auth event log_event failed (non-fatal)", exc_info=True)
            raise HTTPException(status_code=403, detail="Invalid or missing API key")
        return
    # No key configured — fall back to dev_auth_bypass check
    if core.dev_auth_bypass:
        logger.warning(
            "DEV_AUTH_BYPASS=true AND no JARVIS_API_KEY set — ALL authentication "
            "bypassed on %s. DO NOT USE IN PRODUCTION.",
            request.url.path,
        )
        return
    raise HTTPException(
        status_code=401,
        detail="API key not configured. Set JARVIS_API_KEY or enable DEV_MODE.",
    )


async def require_admin(request: Request) -> None:
    """FastAPI dependency — admin-only. Requires an explicit admin session.

    Reads ``request.state.user_role`` set by
    :class:`jarvis_common.session_middleware.SessionMiddleware` when a valid
    session cookie is present.

    WS-AUTH: API-key-only callers (no session ⇒ ``user_role`` absent) are NOT
    admins. The JARVIS_API_KEY is an ops credential, not an admin bearer. Only
    a browser session with ``role == 'admin'`` passes; everything else — no
    session, or a non-admin session — gets 403. For ops endpoints the bot/cron
    legitimately reach with only X-API-Key, use
    :func:`require_admin_or_api_key`.

    Raises
    ------
    fastapi.HTTPException
        403 unless the caller has a session with ``role == 'admin'``.

    """
    role = getattr(request.state, "user_role", None)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


async def require_admin_or_api_key(request: Request) -> None:
    """FastAPI dependency — admin session OR ops API-key caller.

    The previous (lax) ``require_admin`` semantics: callers with no session
    role present (API-key-only: Telegram bot, cron) pass through; only a
    browser session with an explicit non-admin role is rejected with 403.

    Use ONLY on ops endpoints that the bot/cron hit with X-API-Key alone
    (``verify_api_key`` still gates the key itself). User-data routes must
    never depend on this — use :func:`current_user_id_strict` there.
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


async def current_user_id_strict(request: Request) -> int:
    """Return the authenticated user's integer ID, or raise 401.

    Same resolution as :func:`current_user_id` (``request.state.user_id`` via
    :func:`_resolve_request_user_id`) but never returns ``None``: an absent
    identity is a hard 401. Use on user-data routes so an API-key-only caller
    cannot fall through as a permissionless shared user.

    Best-effort audit of the failure (``auth.session.missing``); the 401 is
    raised even if the audit insert fails.
    """
    uid = _resolve_request_user_id(request)
    if uid is None:
        try:
            pool = _request_db_pool(request)
            if pool is not None:
                await log_audit(
                    pool,
                    action="auth.session.missing",
                    resource=request.url.path,
                    metadata={"ip": _client_ip(request)},
                )
        except Exception:  # noqa: BLE001
            logger.debug("auth.session.missing audit failed (non-fatal)", exc_info=True)
        raise HTTPException(status_code=401, detail="Authentication required")
    return uid


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


async def current_user_id_strict_with_owner_override(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
) -> int:
    """Like :func:`current_user_id_with_owner_override` but 401 instead of None.

    Reuses the existing guard logic verbatim (session → X-Owner-User-Id with
    the three guards). When neither a session nor a valid owner override
    resolves an identity, raise 401 rather than returning ``None``.
    """
    uid = await current_user_id_with_owner_override(request, api_key=api_key)
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return uid


async def get_current_user_id(
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> int:
    """Declarative ``Depends`` wrapper for :func:`current_user_id_strict_with_owner_override`.

    CC-03: route handlers historically resolved the caller identity
    imperatively::

        user_id = await current_user_id_strict_with_owner_override(
            request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
        )

    That pattern (a) hides the auth requirement from the OpenAPI schema and
    (b) hand-extracts the ``X-API-Key`` header instead of letting the
    ``APIKeyHeader`` security scheme do it. This thin wrapper lets handlers
    declare ``user_id: int = Depends(get_current_user_id)`` instead: identical
    runtime behaviour (same session → X-Owner-User-Id resolution, same
    401/403), but the dependency is now visible in the generated spec and the
    API-key header is sourced through the declared security scheme.
    """
    return user_id


# allow-user-id-none: legacy Telegram single-tenant path


def validate_production_config() -> None:
    """Crash at startup if production config is unsafe.

    Enforces a set of production-readiness gates so misconfigurations cause a
    loud ``RuntimeError`` at boot rather than silent runtime failures:

    * ``DEV_MODE=true`` is rejected when ``ENVIRONMENT=production``.
    * All granular ``dev_*`` flags are rejected in production.
    * ``JARVIS_API_KEY`` must be set, ≥ 32 characters, and not a placeholder.
    * ``JARVIS_MODEL_HMAC_KEY`` is required in production (M-07; no derivation
      fallback from the API key).
    * ``JARVIS_CONFIG_KEY`` must be set in production (Fernet row-level encrypt).
    * ``LITELLM_MASTER_KEY`` must be strong (SEC-A; rejects known placeholders).
    * ``POSTGRES_PASSWORD`` must be strong (SEC-A; mirrored from readiness-check).
    * ``APP_BASE_URL`` must be set (prevents magic-link host-header poisoning).
    * SMTP (host, port, from) must all be configured in production (H-2).

    Raises
    ------
    RuntimeError
        On the first failed gate encountered.

    """
    from jarvis_common.settings import get_core_settings, get_secrets_settings  # noqa: PLC0415

    core = get_core_settings()
    env = core.environment.lower()
    dev_mode = core.dev_mode
    api_key_secret = get_secrets_settings().jarvis_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret else ""

    if env == "production" and dev_mode:
        raise RuntimeError("DEV_MODE=true is not allowed in ENVIRONMENT=production")

    if env == "production":
        _dev_flag_names = {
            "dev_auth_bypass": core.dev_auth_bypass,
            "dev_error_detail": core.dev_error_detail,
            "dev_cors_open": core.dev_cors_open,
            "dev_smtp_log_only": core.dev_smtp_log_only,
            "dev_crypto_relaxed": core.dev_crypto_relaxed,
        }
        for flag_name, flag_value in _dev_flag_names.items():
            if flag_value:
                raise RuntimeError(f"{flag_name}=true is not allowed in ENVIRONMENT=production")

    if not dev_mode:
        if not api_key or api_key == "CHANGE_ME_REQUIRED":
            raise RuntimeError(
                "JARVIS_API_KEY must be set to a real value (not empty or default sentinel)"
            )
        if len(api_key) < 32:
            raise RuntimeError(
                f"JARVIS_API_KEY must be at least 32 characters (got {len(api_key)})"
            )
        if _is_weak_secret(api_key):
            raise RuntimeError(
                "JARVIS_API_KEY is a known placeholder/weak value — "
                "set a strong secret before deploying to production"
            )

    # H14 / M-07 — Pulse model HMAC key gate. The pulse classifier signs
    # pickle blobs with HMAC-SHA256; without a real key, an attacker with DB
    # write access could forge a signed blob and trigger RCE via pickle.loads.
    # In production, the dedicated ``JARVIS_MODEL_HMAC_KEY`` is mandatory
    # (M-07 — the derivation-from-JARVIS_API_KEY fallback is refused so a
    # stolen bearer cannot also forge model blobs). Require ≥ 32 chars so
    # the signing key has meaningful entropy.
    if env == "production":
        import os as _os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        model_hmac_secret = get_secrets_settings().jarvis_model_hmac_key
        model_hmac_key = model_hmac_secret.get_secret_value() if model_hmac_secret else ""
        if not model_hmac_key:
            raise RuntimeError(
                "JARVIS_MODEL_HMAC_KEY must be set in production "
                "(no derivation fallback). See docs/SECURITY.md#pulse-model-signing."
            )
        if len(model_hmac_key) < 32:
            raise RuntimeError(
                f"JARVIS_MODEL_HMAC_KEY must be at least 32 characters (got {len(model_hmac_key)})"
            )

        # DOM-E-08 — Config encryption key gate. user_config rows are encrypted
        # with Fernet using JARVIS_CONFIG_KEY. Without it the first decrypt at
        # request-time raises a cryptic error instead of a clear boot failure.
        # Always require the key in production so the operator is forced to
        # provision it before any traffic arrives.
        config_key_secret = get_secrets_settings().jarvis_config_key
        config_key = config_key_secret.get_secret_value() if config_key_secret else ""
        if not config_key:
            raise RuntimeError(
                "JARVIS_CONFIG_KEY must be set in production. "
                "Generate with: python -c 'from cryptography.fernet import "
                "Fernet; print(Fernet.generate_key().decode())'"
            )
        if len(config_key) < 32:
            raise RuntimeError(
                f"JARVIS_CONFIG_KEY must be at least 32 characters (got {len(config_key)})"
            )

        # SEC-A — LiteLLM master key gate. Without a strong key a prod VPS can
        # boot with a guessable proxy credential (e.g. the literal
        # ``sk-jarvis-dev-test``), letting anyone who can reach the LiteLLM
        # port spend tokens. Require it set, ≥ 32 chars, and not a known
        # placeholder (placeholder denylist mirrors production-readiness-check.sh).
        litellm_secret = get_secrets_settings().litellm_master_key
        litellm_key = litellm_secret.get_secret_value() if litellm_secret else ""
        if not litellm_key:
            raise RuntimeError("LITELLM_MASTER_KEY must be set in production")
        if _is_weak_secret(litellm_key):
            raise RuntimeError(
                "LITELLM_MASTER_KEY is a known placeholder/weak value — "
                "set a strong secret before deploying to production"
            )
        if len(litellm_key) < _LITELLM_MASTER_KEY_MIN_LEN:
            raise RuntimeError(
                f"LITELLM_MASTER_KEY must be at least "
                f"{_LITELLM_MASTER_KEY_MIN_LEN} characters (got {len(litellm_key)})"
            )

        # SEC-A — PostgreSQL password gate. Mirror the readiness-script
        # resolution order (env var, then the Docker Secret mount) so a
        # secrets-file deployment is not falsely flagged. Reject empty,
        # placeholder, and short passwords.
        postgres_password = _os.environ.get("POSTGRES_PASSWORD", "")
        if not postgres_password:
            try:
                postgres_password = Path(_POSTGRES_PASSWORD_SECRET_PATH).read_text().strip()
            except OSError:
                postgres_password = ""
        if not postgres_password:
            raise RuntimeError("POSTGRES_PASSWORD must be set in production")
        if _is_weak_secret(postgres_password):
            raise RuntimeError(
                "POSTGRES_PASSWORD is a known placeholder/weak value — "
                "set a strong secret before deploying to production"
            )
        if len(postgres_password) < _POSTGRES_PASSWORD_MIN_LEN:
            raise RuntimeError(
                f"POSTGRES_PASSWORD must be at least "
                f"{_POSTGRES_PASSWORD_MIN_LEN} characters (got {len(postgres_password)})"
            )

        # SEC-B — Public base URL gate. Magic-link emails embed APP_BASE_URL;
        # when it is unset the link host falls back to the inbound request
        # ``Host`` header, which an attacker can poison to harvest tokens.
        # Require it explicitly in production.
        app_base_url = _os.environ.get("APP_BASE_URL", "").strip()
        if not app_base_url:
            raise RuntimeError(
                "APP_BASE_URL must be set in production (prevents magic-link host-header poisoning)"
            )

        # H-2 — SMTP gate. When DEV_SMTP_LOG_ONLY is false (already enforced
        # above for production), SMTP is the only delivery path for magic links.
        # An operator who forgets to configure SMTP silently breaks auth — or
        # worse, causes the fallback dev-mode path to log tokens. Require all
        # three minimum SMTP fields so the misconfiguration is caught at boot.
        secrets = get_secrets_settings()
        missing_smtp = [
            field
            for field, value in (
                ("SMTP_HOST", secrets.smtp_host),
                ("SMTP_PORT", secrets.smtp_port),
                ("SMTP_FROM", secrets.smtp_from),
            )
            if value is None
        ]
        if missing_smtp:
            raise RuntimeError(
                f"SMTP must be fully configured in production "
                f"(missing: {', '.join(missing_smtp)}). "
                f"Set SMTP_HOST, SMTP_PORT, and SMTP_FROM, or enable "
                f"DEV_SMTP_LOG_ONLY only in non-production environments."
            )
