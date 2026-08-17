"""Shared API-key, session, and restore-session authentication."""

import hashlib
import hmac
import ipaddress
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import asyncpg
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from jarvis_common.audit import log_audit
from jarvis_common.config import POSTGRES_PASSWORD_SECRET_PATH
from jarvis_common.event_log import log_event
from jarvis_common.paths import read_regular_json_file
from jarvis_common.settings import get_core_settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_HEALTH_PATHS = frozenset({"/health", "/health/", "/health/live", "/healthz", "/health/readiness"})

# Known always-reachable status endpoints that the logged-out frontend polls on
# a loop (dashboard metrics + the two setup gates). Each unauthenticated poll
# would otherwise emit an `auth.session.missing` audit row, drowning the audit
# log in benign noise and burying genuine security events. We skip the AUDIT for
# these paths only — the 401 is still raised, and every other path still audits.
_AUTH_MISSING_AUDIT_SKIP_PATHS = frozenset(
    {
        "/api/dashboard/metrics",
        "/api/system/setup-status",
        "/api/setup/status",
    }
)

# Production secret-strength gate. Minimum lengths mirror the project
# convention enforced by scripts/production-readiness-check.sh so the boot gate
# and the readiness script agree.
_LITELLM_MASTER_KEY_MIN_LEN = 16
_POSTGRES_PASSWORD_MIN_LEN = 16

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
    """Return whether ``value`` is empty or a known placeholder secret.

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


def api_key_matches(presented: str | None, configured: str) -> bool:
    """Compare a presented API key against the configured one in constant time.

    Parameters
    ----------
    presented : str or None
        Candidate key supplied by the caller, usually the ``X-API-Key`` header.
        A missing or empty value compares as a non-match.
    configured : str
        The key this deployment expects.

    Returns
    -------
    bool
        Whether the presented key equals the configured one.

    Notes
    -----
    Callers keep their own "is a key configured?" guard, because they disagree
    about what an unconfigured key means: :func:`verify_api_key` falls through
    to the ``DEV_AUTH_BYPASS`` check, while the owner-override path and the
    API-key-to-session exchange fail closed. Answering that question here would
    silently make all three fail closed.

    """
    return hmac.compare_digest(
        (presented or "").encode("utf-8", "replace"),
        configured.encode("utf-8", "replace"),
    )


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


# --- Restore-scoped browser recovery token ----------------------------------
# The Backup panel returns the raw token once and stores only its v2 hash,
# expiry, restore ID, source, and request timestamp in the trigger volume. The
# browser may reuse it to poll the same restore after restored sessions vanish.
# An inbox-bound token may also authenticate the exact acknowledgement route,
# which revalidates it against quarantine and consumes it atomically.
RESTORE_STATUS_PATH = "/api/admin/backups/restore/status"
RESTORE_ACKNOWLEDGE_PATH = "/api/admin/backups/restore/acknowledge"
_RESTORE_STATUS_TOKEN_FILENAME = ".restore_status_token.json"


@dataclass(frozen=True, slots=True)
class RestoreStatusTokenRecord:
    """Validated database-independent record for one restore session.

    Attributes
    ----------
    sha256 : str
        Lowercase hash of the raw token. The raw value is never persisted on
        the server; the initiating browser may retain it in session storage.
    expires_at : datetime
        Aware expiry instant used for both polling and acknowledgement auth.
    restore_id : str
        Lowercase 128-bit identifier generated for this restore request.
    source : {"local", "inbox"}
        ``local`` for same-host rollback or ``inbox`` for off-host recovery.
    requested_at : str
        Original aware timestamp string also written into the restore request and
        later quarantine record.

    Notes
    -----
    Parsing validates this record's schema, timestamps, and allowed source. The
    bearer helpers authenticate a presented raw token against ``sha256``; the
    acknowledgement route performs the final binding to durable quarantine.

    """

    sha256: str
    expires_at: datetime
    restore_id: str
    source: Literal["local", "inbox"]
    requested_at: str


def restore_status_token_file() -> Path:
    """Resolve the restore-session record path from the active trigger directory.

    Returns
    -------
    pathlib.Path
        Path to the version-two hashed token record. Resolution happens per call so
        isolated tests and alternate deployments can set ``BACKUP_TRIGGER_DIR``.

    """
    trigger_dir = os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")
    return Path(trigger_dir) / _RESTORE_STATUS_TOKEN_FILENAME


def _parse_restore_status_token_record(data: object) -> RestoreStatusTokenRecord:
    """Validate and convert a decoded restore-session record.

    Parameters
    ----------
    data : object
        JSON value read from the restore-session record.

    Returns
    -------
    RestoreStatusTokenRecord
        Validated, unexpired version-two record.

    Raises
    ------
    TypeError
        If a field has an invalid type.
    ValueError
        If the schema, identifier, source, or timestamp is invalid.

    """
    expected = {
        "version",
        "sha256",
        "expires_at",
        "restore_id",
        "source",
        "requested_at",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("invalid restore-session schema")

    stored_hash = data.get("sha256")
    expires_at = data.get("expires_at")
    restore_id = data.get("restore_id")
    source = data.get("source")
    requested_at = data.get("requested_at")
    if (
        not isinstance(stored_hash, str)
        or not isinstance(expires_at, str)
        or not isinstance(restore_id, str)
        or not isinstance(source, str)
        or not isinstance(requested_at, str)
    ):
        raise TypeError("restore-session fields must be strings")
    if (
        data.get("version") != 2
        or re.fullmatch(r"[0-9a-f]{64}", stored_hash) is None
        or re.fullmatch(r"[0-9a-f]{32}", restore_id) is None
        or source not in {"local", "inbox"}
    ):
        raise ValueError("invalid restore-session field")

    expiry = datetime.fromisoformat(expires_at)
    requested = datetime.fromisoformat(requested_at)
    if expiry.tzinfo is None or requested.tzinfo is None:
        raise ValueError("restore-session timestamps must include time zones")
    if datetime.now(UTC) > expiry or requested >= expiry:
        raise ValueError("restore-session timestamps are outside their valid interval")
    return RestoreStatusTokenRecord(
        sha256=stored_hash,
        expires_at=expiry,
        restore_id=restore_id,
        source=cast(Literal["local", "inbox"], source),
        requested_at=requested_at,
    )


def read_restore_status_token_record() -> RestoreStatusTokenRecord | None:
    """Return the stored, unexpired restore-session record, or ``None``.

    Validation does not access the database and fails closed: linked, oversized,
    missing, malformed, expired, or schema-drifted state is unusable and never
    raises through the authentication boundary.

    Returns
    -------
    RestoreStatusTokenRecord or None
        The exact version-two record when every field and timestamp is valid and the
        expiry is still in the future; otherwise ``None``.

    Notes
    -----
    Parsing alone does not authenticate a browser or bind the record to current
    quarantine. The bearer helpers perform the hash comparison. Acknowledgement
    must then re-read the record under the restore-state lock, match the current
    off-host quarantine, and consume it before clearing quarantine.

    """
    try:
        data = read_regular_json_file(restore_status_token_file())
        return _parse_restore_status_token_record(data)
    except (OSError, ValueError, TypeError):
        return None


def _restore_bearer_valid(
    request: Request,
    *,
    required_source: Literal["local", "inbox"] | None = None,
) -> bool:
    """Validate the presented bearer against the current restore-session record."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    token_record = read_restore_status_token_record()
    if token_record is None or (
        required_source is not None and token_record.source != required_source
    ):
        return False
    presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(presented, token_record.sha256)


def restore_status_bearer_valid(request: Request) -> bool:
    """Return whether the request carries a valid restore-session token.

    The check reads only the trigger-volume hash record, computes the presented
    token's SHA-256 digest, and compares it in constant time. Missing, expired,
    malformed, or non-bearer input returns ``False`` without raising.

    Parameters
    ----------
    request : Request
        Request whose ``Authorization`` header may carry the raw bearer.

    Returns
    -------
    bool
        Whether the header matches the current unexpired restore session.

    """
    return _restore_bearer_valid(request)


def restore_acknowledgement_bearer_valid(request: Request) -> bool:
    """Validate a bearer against the current inbox-bound restore session.

    Parameters
    ----------
    request : Request
        Request whose bearer is checked without database access.

    Returns
    -------
    bool
        Whether the current token is valid, unexpired, and bound to an
        off-host restore.

    Notes
    -----
    This helper does not inspect the HTTP method or path. :func:`verify_api_key`
    restricts its use to the exact acknowledgement request. Route authorization
    still binds the restore ID and request timestamp to quarantine and consumes
    the token record under the shared restore-state lock.

    """
    return _restore_bearer_valid(request, required_source="inbox")


# System ``user_config`` row (user_id NULL) that lets an admin flip the
# multi-tenant API-key-login gate in-app — the recovery path before the boot
# API key would otherwise be the only credential. Read as ``env default OR DB
# override`` so the operator can enable it post-deploy without an env edit. The
# key string lives here (jarvis_common) so the read side owns it; the
# paper_ingestion config allowlist imports this constant to register the write.
API_KEY_LOGIN_CONFIG_KEY = "auth.api_key_login_enabled"

# Module-level cache of the DB override. ``None`` means "not yet read / just
# invalidated"; once read it caches True/False. Single-process uvicorn, so this
# mirrors the _CACHED_API_KEY single-process assumption. The flag only WIDENS
# access, so an admin flip OFF must invalidate promptly — settings write does so.
_api_key_login_db_override: bool | None = None


def invalidate_api_key_login_cache() -> None:
    """Drop the cached DB override (call after an admin flips the flag).

    Mirror of :func:`invalidate_effective_num_ctx_cache`: the next
    :func:`api_key_login_enabled` read re-resolves the system row.
    """
    global _api_key_login_db_override
    _api_key_login_db_override = None


async def api_key_login_enabled(conn: asyncpg.Connection) -> bool:
    """Resolve the effective multi-tenant API-key-login flag.

    ``env default OR DB override``: the env ``API_KEY_LOGIN_ENABLED`` short-
    circuits to True when set (operators who hard-enable it in compose env keep
    that behaviour); otherwise the in-app admin toggle persisted to the
    ``auth.api_key_login_enabled`` system row decides. The DB read is cached in
    process and invalidated by :func:`invalidate_api_key_login_cache` on write.
    """
    if get_core_settings().api_key_login_enabled:
        return True
    global _api_key_login_db_override
    if _api_key_login_db_override is None:
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
            API_KEY_LOGIN_CONFIG_KEY,
        )
        _api_key_login_db_override = bool(row["value"]) if row is not None else False
    return _api_key_login_db_override


async def verify_api_key(request: Request, api_key: str | None = Depends(_api_key_header)) -> None:
    """Enforce application-wide request authentication.

    Parameters
    ----------
    request : fastapi.Request
        Incoming request whose path, method, session state, and restore bearer
        determine how it is authenticated.
    api_key : str or None
        Optional ``X-API-Key`` value injected by FastAPI.

    Raises
    ------
    fastapi.HTTPException
        With status 403 when no permitted exception, valid browser session,
        configured API key, or development-mode fallback authorizes the request.

    Notes
    -----
    Health, infrastructure-event, sign-in, first-run setup, and
    ``GET /api/account`` requests use their route-specific authorization.
    Restore status and inbox acknowledgement accept an exact restore token
    without database access. A configured API key is enforced in every environment; the
    module-level cache avoids reading the secret on every request.

    """
    jarvis_api_key = _CACHED_API_KEY
    core = get_core_settings()
    if request.url.path in _HEALTH_PATHS:
        return
    # Sign-in, verification, and logout routes perform their own authentication.
    # These endpoints have their own validation (token TTL + single-use).
    if request.url.path.startswith("/api/auth/"):
        return
    # The dashboard uses this endpoint to resolve its current browser session.
    # The route requires current_user_id_strict, so anonymous requests receive
    # 401 without reading account data. Mutating methods still require the
    # standard API or session authentication.
    if request.url.path == "/api/account" and request.method == "GET":
        return
    # Setup routes permit initial configuration before a browser session exists,
    # then require an administrator after setup completes.
    if request.url.path.startswith("/api/setup/"):
        return
    # Restore status accepts a token from either source. Acknowledgement accepts
    # only an inbox-bound token, then revalidates and consumes it against the
    # current quarantine record.
    request_method = getattr(request, "method", "").upper()
    restore_token_request = (
        request_method == "GET"
        and request.url.path == RESTORE_STATUS_PATH
        and restore_status_bearer_valid(request)
    ) or (
        request_method == "POST"
        and request.url.path == RESTORE_ACKNOWLEDGE_PATH
        and restore_acknowledgement_bearer_valid(request)
    )
    if restore_token_request:
        return
    # SessionMiddleware sets user_id only for a live, unexpired session whose
    # user still exists. Routes independently resolve the user's identity and
    # role; a browser session does not grant operations or administrator access.
    if getattr(getattr(request, "state", None), "user_id", None) is not None:
        return
    # A configured API key is enforced in every environment.
    if jarvis_api_key:
        if not api_key_matches(api_key, jarvis_api_key):
            # Record failures as potential probes or client misconfiguration.
            # Successful checks are omitted to avoid per-request log noise.
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
                logger.warning("auth event log failed (non-fatal)", exc_info=True)
            raise HTTPException(status_code=403, detail="Invalid or missing API key")
        return
    # No key configured — fall back to dev_auth_bypass check
    if core.dev_auth_bypass:
        logger.warning(
            "DEV_AUTH_BYPASS=true with no JARVIS_API_KEY bypasses authentication "
            "on %s; this configuration is unsafe for production.",
            request.url.path,
        )
        return
    raise HTTPException(
        status_code=401,
        detail="API key not configured. Set JARVIS_API_KEY or enable DEV_MODE.",
    )


async def require_admin(request: Request) -> None:
    """Require an explicit administrator session.

    Parameters
    ----------
    request : Request
        Request whose session middleware state contains the authenticated role.

    Raises
    ------
    HTTPException
        If the request has no administrator session.

    Notes
    -----
    An operations API key alone is not an administrator credential. Operations
    endpoints that intentionally support that key use
    :func:`require_admin_or_api_key`.

    """
    role = getattr(request.state, "user_role", None)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


async def require_admin_or_api_key(request: Request) -> None:
    """Allow an administrator session or a verified operations API-key caller.

    Parameters
    ----------
    request : Request
        Request whose session role, when present, must be ``"admin"``.

    Raises
    ------
    HTTPException
        If an authenticated browser session has a non-administrator role.

    Notes
    -----
    :func:`verify_api_key` validates API-key callers before this dependency.
    User-data routes use :func:`current_user_id_strict` instead.

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


async def current_user_id_or_none(request: Request) -> int | None:
    """Return the authenticated user ID when one is present, otherwise ``None``.

    Prefer this name in ``Depends(...)`` injection points so the call-site
    reads "I know this can be None and I handle it." Same body as
    It reads ``request.state.user_id`` set by the session middleware, with a
    defensive fallback to ``None``.
    """
    return _resolve_request_user_id(request)


async def current_user_id_strict(request: Request) -> int:
    """Return the authenticated user's integer ID, or raise 401.

    Same resolution as :func:`current_user_id_or_none` (``request.state.user_id`` via
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
            if pool is not None and request.url.path not in _AUTH_MISSING_AUDIT_SKIP_PATHS:
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
# X-Owner-User-Id override — Telegram per-user orchestration
# ---------------------------------------------------------------------------

_OWNER_OVERRIDE_HEADER = "X-Owner-User-Id"

# ASGI scope key under which app_factory.RawClientStashMiddleware snapshots the
# ORIGINAL ``scope["client"]`` (the real transport peer) BEFORE uvicorn's
# ProxyHeadersMiddleware rewrites ``scope["client"]`` in place from
# X-Forwarded-For. By the time a route dependency runs, ``request.client``
# already reflects the (caller-controllable) XFF chain — only this stash still
# holds the actual socket peer for authorization and audit.
RAW_CLIENT_SCOPE_KEY = "jarvis.raw_client"


def _raw_socket_ip(request: Request) -> tuple[str | None, bool]:
    """Return ``(raw_peer_ip, stashed)`` from the RawClientStashMiddleware snapshot.

    ``stashed`` is True when the middleware ran for this request — i.e. the
    scope KEY is present, even if the transport reported no client (then
    ``raw_peer_ip`` is None and the allowlist check fails safe). When the key
    is absent, the app was built without
    :func:`jarvis_common.app_factory.configure_middleware_and_errors`
    (e.g. a bare test app); such apps install no ProxyHeadersMiddleware either,
    so the caller may safely fall back to ``request.client``.
    """
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict) or RAW_CLIENT_SCOPE_KEY not in scope:
        return None, False
    raw = scope[RAW_CLIENT_SCOPE_KEY]
    if isinstance(raw, tuple | list) and raw and isinstance(raw[0], str):
        return raw[0], True
    return None, True


def _parse_allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse ``OWNER_OVERRIDE_ALLOWED_CIDRS`` env var into network objects.

    Falls back to loopback-only (``127.0.0.0/8``) by default when the variable
    is unset or empty; containerized/bridge callers must set
    ``OWNER_OVERRIDE_ALLOWED_CIDRS``.
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


# Parsed once on first use (or on explicit refresh); avoids re-parsing CIDRs per request.
_CACHED_ALLOWED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None

# Deny-by-default code value (mirrors CoreSettings.owner_override_allowed_cidrs).
# When the resolved setting still equals this, the operator has not widened the
# allowlist, so only loopback callers can use X-Owner-User-Id.
_LOOPBACK_ONLY_DEFAULT = "127.0.0.0/8"
# Guards the startup warning so it fires at most once per process.
_LOOPBACK_DEFAULT_WARNED = False


def refresh_allowed_networks_cache() -> None:
    """Re-parse ``OWNER_OVERRIDE_ALLOWED_CIDRS`` and update the module-level cache.

    Mirror of :func:`refresh_api_key_cache`.  Call this at app lifespan startup
    (or in tests that monkeypatch ``OWNER_OVERRIDE_ALLOWED_CIDRS``) so the
    cached value reflects the current environment.

    Emits a one-time warning when the allowlist is still the loopback-only
    default — a non-loopback caller (e.g. a containerized bot on a bridge
    network) would then be denied unless the operator widens it.
    """
    global _CACHED_ALLOWED_NETWORKS, _LOOPBACK_DEFAULT_WARNED
    _CACHED_ALLOWED_NETWORKS = _parse_allowed_networks()
    if (
        not _LOOPBACK_DEFAULT_WARNED
        and get_core_settings().owner_override_allowed_cidrs.strip() == _LOOPBACK_ONLY_DEFAULT
    ):
        _LOOPBACK_DEFAULT_WARNED = True
        logger.warning(
            "OWNER_OVERRIDE_ALLOWED_CIDRS is the loopback-only default (%s): "
            "X-Owner-User-Id override is restricted to loopback. Non-loopback "
            "callers (e.g. a containerized bot on a bridge network) must set "
            "OWNER_OVERRIDE_ALLOWED_CIDRS to include their source range.",
            _LOOPBACK_ONLY_DEFAULT,
        )


def _ip_in_allowlist(ip_str: str | None) -> bool:
    """Return True when *ip_str* falls within one of the allowed CIDRs."""
    global _CACHED_ALLOWED_NETWORKS
    if not ip_str:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if _CACHED_ALLOWED_NETWORKS is None:
        _CACHED_ALLOWED_NETWORKS = _parse_allowed_networks()
    for net in _CACHED_ALLOWED_NETWORKS:
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
       b. BOTH the raw transport peer (stashed by
          ``app_factory.RawClientStashMiddleware`` before ProxyHeadersMiddleware
          rewrites ``scope["client"]`` from X-Forwarded-For) AND
          ``request.client`` are within the allowlist (loopback-only
          (``127.0.0.0/8``) by default; containerized/bridge callers must set
          ``OWNER_OVERRIDE_ALLOWED_CIDRS``).
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
    if not jarvis_api_key or not api_key_matches(api_key, jarvis_api_key):
        logger.warning(
            "X-Owner-User-Id header present but API key check failed from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=403,
            detail="X-Owner-User-Id requires a valid X-API-Key",
        )

    # Guard (b): source IP must be in the allowlist — required for BOTH:
    #   * request.client, which uvicorn's ProxyHeadersMiddleware has ALREADY
    #     rewritten in place from X-Forwarded-For by the time this dependency
    #     runs (on its own it can reflect a caller-forged header), AND
    #   * the raw transport peer stashed by RawClientStashMiddleware BEFORE
    #     that rewrite (the real socket address — unforgeable).
    # Requiring BOTH keeps the bridge bot working (no XFF → both values are the
    # bridge IP → allowed) while rejecting a forged X-Forwarded-For from a
    # non-allowlisted peer (raw peer = attacker IP) AND the nginx-relayed
    # browser path (rewritten client = public browser IP). Strictly tighter
    # than either check alone.
    client_ip = request.client.host if request.client else None
    raw_ip, raw_stashed = _raw_socket_ip(request)
    if not raw_stashed:
        # Stash absent ⇒ app built without configure_middleware_and_errors
        # (no RawClientStashMiddleware — e.g. minimal test apps). Those apps
        # install no ProxyHeadersMiddleware either, so request.client IS the
        # raw socket peer: fall back to the single direct-peer check on it.
        raw_ip = client_ip
    if not (_ip_in_allowlist(client_ip) and _ip_in_allowlist(raw_ip)):
        logger.warning(
            "X-Owner-User-Id header rejected: client IP %s / raw socket peer %s "
            "not both in allowlist",
            client_ip,
            raw_ip,
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
    try:
        pool = _request_db_pool(request)
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

    # Emit an audit event on every successful override so the
    # operator can detect unexpected per-user identity substitution.  Best-effort:
    # a transient pool failure must never block the request.
    try:
        audit_pool = _request_db_pool(request)
        if audit_pool is not None:
            await log_audit(
                audit_pool,
                action="auth.owner_override.used",
                resource=request.url.path,
                user_id=str(override_uid),
                metadata={
                    "client_ip": client_ip or "unknown",
                    # Record the raw socket peer so the audit trail distinguishes
                    # the transport source from the XFF-rewritable client address.
                    "raw_client_ip": raw_ip or "unknown",
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug("auth.owner_override audit log failed (non-fatal)", exc_info=True)

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
    user_id: int = Depends(current_user_id_strict),
) -> int:
    """Return the session-authenticated caller identity, or raise 401.

    Parameters
    ----------
    user_id : int
        Identity supplied by :func:`current_user_id_strict`.

    Returns
    -------
    int
        Authenticated caller identity.

    Notes
    -----
    The default identity dependency for user-data routes. It deliberately does
    NOT honour the ``X-Owner-User-Id`` override: in ``paper_ingestion`` only the
    routes the Telegram bot actually calls declare
    :func:`get_current_user_id_or_bot`, and an exact-set test pins that surface.
    ``learning_engine`` routes still declare
    :func:`current_user_id_strict_with_owner_override` directly, so the header
    resolves there on a wider surface than the bot uses; the address allowlist,
    not this dependency, is what bounds it.

    """
    return user_id


async def get_current_user_id_or_bot(
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> int:
    """Return the caller identity, honouring the ``X-Owner-User-Id`` override.

    Parameters
    ----------
    user_id : int
        Identity supplied by
        :func:`current_user_id_strict_with_owner_override`.

    Returns
    -------
    int
        Authenticated caller identity — possibly the user the
        service-authenticated Telegram bot is acting for.

    Notes
    -----
    Reserved for the routes the Telegram bot calls on a user's behalf.
    Declaring it through ``Depends`` exposes the API-key security scheme in
    OpenAPI while preserving session and owner-override resolution.

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
    * ``JARVIS_MODEL_HMAC_KEY`` is required in production (no derivation
      fallback from the API key).
    * ``JARVIS_CONFIG_KEY`` must be set in production (Fernet row-level encrypt).
    * ``LITELLM_MASTER_KEY`` must be strong (rejects known placeholders).
    * The PostgreSQL password env value or configured role-scoped file must be strong.
    * ``APP_BASE_URL`` must be set (prevents magic-link host-header poisoning).

    Raises
    ------
    RuntimeError
        On the first failed gate encountered.

    """
    from jarvis_common.settings import get_secrets_settings  # noqa: PLC0415

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

    # Pulse model HMAC key gate. The pulse classifier signs
    # pickle blobs with HMAC-SHA256; without a real key, an attacker with DB
    # write access could forge a signed blob and trigger RCE via pickle.loads.
    # The dedicated ``JARVIS_MODEL_HMAC_KEY`` is mandatory whenever the
    # derivation-from-JARVIS_API_KEY fallback would let a stolen bearer also
    # forge model blobs: in production AND on any multi-user deployment
    # (``JARVIS_SETUP_MODE != single``, e.g. an internal multi-user box).
    # Require ≥ 32 chars so the signing key has meaningful entropy.
    if env == "production" or core.jarvis_setup_mode != "single":
        model_hmac_secret = get_secrets_settings().jarvis_model_hmac_key
        model_hmac_key = model_hmac_secret.get_secret_value() if model_hmac_secret else ""
        if not model_hmac_key:
            raise RuntimeError(
                "JARVIS_MODEL_HMAC_KEY must be set for production or multi-user "
                "deployments (no derivation fallback). "
                "See docs/SECURITY.md#pulse-model-signing."
            )
        if len(model_hmac_key) < 32:
            raise RuntimeError(
                f"JARVIS_MODEL_HMAC_KEY must be at least 32 characters (got {len(model_hmac_key)})"
            )

    if env == "production":
        # Config encryption key gate. user_config rows are encrypted
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

        # LiteLLM master key gate. Without a strong key a prod VPS can
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

        # PostgreSQL password gate. Mirror the readiness-script
        # resolution order (env var, then the Docker Secret mount) so a
        # secrets-file deployment is not falsely flagged. Reject empty,
        # placeholder, and short passwords.
        postgres_password = os.environ.get("POSTGRES_PASSWORD", "")
        if not postgres_password:
            password_file = os.environ.get("POSTGRES_PASSWORD_FILE", POSTGRES_PASSWORD_SECRET_PATH)
            try:
                postgres_password = Path(password_file).read_text().strip()
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

        # Public base URL gate. Magic-link emails embed APP_BASE_URL;
        # when it is unset the link host falls back to the inbound request
        # ``Host`` header, which an attacker can poison to harvest tokens.
        # Require it explicitly in production.
        app_base_url = os.environ.get("APP_BASE_URL", "").strip()
        if not app_base_url:
            raise RuntimeError(
                "APP_BASE_URL must be set in production (prevents magic-link host-header poisoning)"
            )


async def validate_runtime_config(
    pool: Any, *, environment: str, setup_token_set: bool, model_hmac_ok: bool
) -> None:
    """Validate authentication requirements that depend on database state.

    Parameters
    ----------
    pool : Any
        Database pool used to count active users and administrators and inspect
        effective SMTP configuration.
    environment : str
        Runtime environment name; production applies the first-admin safeguard.
    setup_token_set : bool
        Whether first-admin setup requires the configured host token.
    model_hmac_ok : bool
        Whether the Pulse model-signing key satisfies the multi-user minimum.

    Raises
    ------
    RuntimeError
        If a multi-user deployment lacks a valid model-signing key, or a
        production deployment has neither an administrator nor a setup token.

    Notes
    -----
    An unprotected non-production first-admin window and unavailable SMTP on a
    multi-user production deployment are logged as warnings.

    """
    env = environment.lower()
    async with pool.acquire() as conn:
        user_count = int(
            await conn.fetchval("SELECT count(*) FROM users WHERE deleted_at IS NULL") or 0
        )
        admin_count = int(
            await conn.fetchval(
                "SELECT count(*) FROM users WHERE role = 'admin' AND deleted_at IS NULL"
            )
            or 0
        )
    multi_user = user_count > 1
    if multi_user and not model_hmac_ok:
        raise RuntimeError(
            "JARVIS_MODEL_HMAC_KEY (>=32 chars) is required once more than one "
            "user exists (Pulse model-signing key cannot be derived on a "
            "multi-user deployment). See docs/SECURITY.md#pulse-model-signing."
        )
    if admin_count == 0 and not setup_token_set:
        if env == "production":
            raise RuntimeError(
                "JARVIS_SETUP_TOKEN must be set on a production deployment with no "
                "admin yet (prevents unauthenticated first-admin takeover)."
            )
        logger.warning(
            "First-admin setup window is unprotected: no admin user exists and "
            "JARVIS_SETUP_TOKEN is not set. Anyone who can reach this instance can "
            "create the first admin. Set JARVIS_SETUP_TOKEN before exposing it."
        )
    if env == "production" and multi_user:
        from jarvis_common.email import effective_smtp_status  # noqa: PLC0415

        deliverable, _issues = await effective_smtp_status(pool)
        if not deliverable:
            logger.warning(
                "SMTP is not deliverable on a multi-user production deployment; "
                "magic-link sign-in is unavailable for non-owner users until SMTP "
                "is configured (env or the setup wizard). Issues: %s",
                _issues,
            )
