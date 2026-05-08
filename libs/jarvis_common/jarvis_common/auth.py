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


def _resolve_single_tenant_user_id() -> None:  # type: ignore[return]
    """Single-tenant placeholder — returns None until Wave-6 ships a real resolver."""
    return None  # allow-user-id-none: pre-Wave-6 single-tenant


async def current_user_id(request: Request) -> int | None:
    """Return the current user's integer ID, or None in single-tenant mode.

    Placeholder: multi-user support is not implemented yet.  Always returns
    None so that user_id ownership checks are a no-op for single-tenant
    deployments while remaining forward-compatible with future multi-user work.

    .. warning::
        TODO(Wave-6): Replace this with real ownership helpers once multi-tenant
        auth is implemented.  Do NOT add new business logic that depends on this
        returning a real user ID — it will always be None until Wave-6 ships.
        Use :func:`current_user_id_or_none` for new code to make the intent
        explicit, and :func:`assert_multi_tenant_not_implemented` to guard paths
        that must not run in single-tenant mode.
    """
    # SEC-108: single-tenant placeholder — always None until Wave-6.
    return _resolve_single_tenant_user_id()


async def current_user_id_or_none(request: Request) -> int | None:
    """Safe single-tenant alias for :func:`current_user_id`.

    Prefer this function for new ``Depends(...)`` injection points so that the
    call-site intent is explicit: "I know this can be None and I handle it."
    Returns None in all current deployments (single-tenant).
    """
    return _resolve_single_tenant_user_id()


def assert_multi_tenant_not_implemented() -> None:
    """Raise ``NotImplementedError`` to guard code paths requiring real user IDs.

    Call this inside functions that MUST have a real user identity to work
    correctly — e.g. cross-user data isolation, ownership transfer, or any
    path that would be a privilege escalation bug if user_id is None.

    Raises
    ------
    NotImplementedError
        Always, until Wave-6 multi-tenant auth is implemented.
    """
    # TODO(Wave-6): remove this guard once multi-tenant auth ships.
    raise NotImplementedError("multi-tenant auth not yet implemented; use Wave-6 ownership helpers")


def single_tenant_user_id() -> None:  # type: ignore[return]
    """Return the implicit user-id for single-tenant (pre-Wave-4) callers.

    In the current single-tenant deployment every authenticated request belongs
    to the one configured owner, which is represented in the DB as ``None`` for
    ``user_id`` columns (matched via ``IS NOT DISTINCT FROM NULL``).

    Returns
    -------
    None
        Always ``None`` — the single-tenant sentinel value. Wave-4 will replace
        calls to this function with real user-id resolution from the pairing table.
    """
    return _resolve_single_tenant_user_id()  # allow-user-id-none: pre-Wave-6 single-tenant


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
