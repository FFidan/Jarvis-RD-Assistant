"""Rate limiting shared across JARVIS services."""

import ipaddress
import logging
from collections.abc import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from jarvis_common.config import get_jarvis_common_settings

logger = logging.getLogger(__name__)

# Trusted proxy CIDRs loaded once at import time.
# Override / extend via TRUSTED_PROXY_CIDRS env var (comma-separated CIDRs).
# Defaults include Docker bridge / RFC-1918 ranges so Docker-internal hops are
# always skipped when walking X-Forwarded-For right-to-left.
_DEFAULT_PROXY_CIDRS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",  # Docker default bridge
    "192.168.0.0/16",
]


def _build_trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Build the trusted proxy network list from env + defaults.

    When ``TRUSTED_PROXY_CIDRS`` is non-empty the env value is used **exclusively**
    (override semantics).  This lets operators narrow the trusted range to
    deployment-specific CIDRs without the broad RFC-1918 defaults leaking in.
    When the env var is absent or empty the built-in ``_DEFAULT_PROXY_CIDRS`` are
    used as a safe fallback.
    """
    settings = get_jarvis_common_settings()
    cidrs = settings.trusted_proxy_cidrs_list or _DEFAULT_PROXY_CIDRS
    return [ipaddress.ip_network(c.strip()) for c in cidrs if c.strip()]


_TRUSTED_PROXIES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = _build_trusted_proxies()


def refresh_trusted_proxies() -> None:
    """Re-build the trusted proxy list from the current environment.

    Tests that monkeypatch TRUSTED_PROXY_CIDRS after import must call this so
    the module-level cache reflects the new environment.  Mirrors the
    ``auth.refresh_api_key_cache()`` pattern.
    """
    global _TRUSTED_PROXIES
    _TRUSTED_PROXIES = _build_trusted_proxies()


def _is_trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* falls within any trusted proxy CIDR."""
    return any(addr in net for net in _TRUSTED_PROXIES)


def _real_ip(request: Request) -> str:
    """Return the real client IP using Werkzeug-style right-to-left XFF walk.

    Algorithm:
    1. If JARVIS_TRUST_CF_CONNECTING_IP=true and CF-Connecting-IP header is set, use it.
       (SEC-006: header is only trusted when the operator has explicitly opted in,
        preventing LAN attackers from forging it when Cloudflare is not in the path.)
    2. Else walk X-Forwarded-For right-to-left, skipping contiguous trusted proxies
       at the tail.  Return the first non-trusted entry (the real client).
       (SEC-001: left-to-right walk allowed a LAN attacker to prepend a fake IP and
        bypass rate limiting; right-to-left is immune to that spoofing.)
    3. If all entries in XFF are trusted (pathological), return request.client.host.
    4. If no XFF header, return request.client.host.
    """
    # SEC-006: CF-Connecting-IP only trusted when operator explicitly enables it,
    # and only when it is a single well-formed IP. A malformed value (e.g. a
    # forged comma-separated list) must not be trusted — fall through to the
    # validated XFF walk instead of crashing or honouring the bad value.
    if get_jarvis_common_settings().trust_cf_connecting_ip:
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            candidate = cf_ip.strip()
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                # malformed → fall through to XFF validation below.
                # Log: with trust opted in, a malformed value is an operational
                # anomaly (misconfigured proxy or a forging attempt) worth surfacing.
                logger.warning(
                    "Malformed CF-Connecting-IP header ignored (not a single IP): %r",
                    candidate,
                )
            else:
                return candidate

    xff = request.headers.get("X-Forwarded-For")
    if not xff:
        return request.client.host if request.client else "unknown"

    # Parse and walk right-to-left (SEC-001 fix).
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            # Malformed hop — attacker-controlled string; do NOT use it as a
            # rate-limit key (distinct forged strings would let one source bypass
            # per-IP limits).  Fall back to the socket peer instead.
            return request.client.host if request.client else "unknown"
        if not _is_trusted(ip):
            return hop

    # All hops were trusted proxies — fall back to socket peer.
    return request.client.host if request.client else "unknown"


def _user_or_ip_key(request: Request) -> str:
    """Rate-limit key: ``user:<id>`` for authenticated requests, ``ip:<addr>`` otherwise.

    Authenticated callers (session cookie resolved by SessionMiddleware → an
    integer ``request.state.user_id``) each get an independent quota bucket so
    one user cannot exhaust the shared IP bucket that every user behind a
    reverse proxy would otherwise share.

    Unauthenticated callers (no valid session, e.g. ``/api/auth/*`` endpoints)
    fall back to the real client IP so anti-enumeration limits are still
    enforced per-IP.
    """
    uid = getattr(request.state, "user_id", None)
    if isinstance(uid, int):
        return f"user:{uid}"
    return f"ip:{_real_ip(request)}"


def create_limiter(
    default_limits: list[str | Callable[..., str]] | None = None,
    *,
    user_aware: bool = True,
) -> Limiter:
    """Create a rate limiter keyed by user or IP.

    Parameters
    ----------
    default_limits:
        Optional list of limit strings applied as a global cap on every
        request (e.g. ``["600/minute"]``).  These are enforced by
        ``SlowAPIMiddleware`` before any route-level auth takes effect,
        providing a first-line defence against unauthenticated brute-force.
        Defaults to ``["600/minute"]`` when not specified.
    user_aware:
        When ``True`` (default) the key function returns ``user:<id>`` for
        authenticated requests and ``ip:<addr>`` for unauthenticated ones.
        Set to ``False`` to force pure IP-based keying (legacy behaviour).

    """
    if default_limits is None:
        default_limits = ["600/minute"]
    key_func = _user_or_ip_key if user_aware else _real_ip
    return Limiter(key_func=key_func, default_limits=default_limits)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Handle rate limit exceeded errors with a JSON 429 response.

    RFC 6585 §4 requires Retry-After header on 429 responses to indicate
    when the client may retry. We extract the granularity period from the
    limit's GRANULARITY.seconds (e.g. 60 for "5/minute").
    """
    _ = request  # Starlette requires (request, exc) interface; path not needed in 429 body

    # Extract reset_seconds from exc.limit.limit.GRANULARITY.seconds, with safe fallback.
    reset_seconds = 60
    try:
        if exc.limit and exc.limit.limit and hasattr(exc.limit.limit, "GRANULARITY"):
            reset_seconds = exc.limit.limit.GRANULARITY.seconds
    except (AttributeError, TypeError):
        # Fallback to 60s if structure is unexpected; this is a safety net.
        pass

    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded ({exc.limit}). Please try again later."},
        headers={"Retry-After": str(reset_seconds)},
    )
