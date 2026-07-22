"""Rate limiting shared across JARVIS services."""

import ipaddress
import logging
from collections.abc import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from jarvis_common.auth import RAW_CLIENT_SCOPE_KEY
from jarvis_common.config import get_jarvis_common_settings

logger = logging.getLogger(__name__)

# Trusted proxy CIDRs loaded once at import time.
# Default trusts loopback only, so a container on a Docker bridge cannot spoof
# X-Forwarded-For to control the rate-limit key. Deployments behind a reverse
# proxy must allowlist only that proxy's exact source address. Compose derives
# the dashboard proxy's pinned /32 from JARVIS_DASHBOARD_IP; a non-empty value
# overrides this list exclusively.
_DEFAULT_PROXY_CIDRS = [
    "127.0.0.0/8",
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


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return a parsed IP address, or ``None`` for malformed input."""
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _test_double_peer(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return an explicit peer from a scope-less request test double."""
    client_host = request.client.host if request.client else None
    return _parse_ip(client_host) if isinstance(client_host, str) else None


def _stashed_raw_peer(scope: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the raw peer stashed on a production ASGI scope."""
    if not isinstance(scope, dict) or RAW_CLIENT_SCOPE_KEY not in scope:
        return None
    raw_peer = scope[RAW_CLIENT_SCOPE_KEY]
    if not isinstance(raw_peer, tuple | list) or not raw_peer or not isinstance(raw_peer[0], str):
        return None
    return _parse_ip(raw_peer[0])


def _transport_peer(
    request: Request,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the authoritative transport peer, or ``None`` when it is unsafe.

    Real ASGI requests have a ``scope`` dict and must carry the snapshot written
    by ``RawClientStashMiddleware``. A missing or malformed snapshot fails
    closed because proxy middleware may already have rewritten
    ``request.client``. Tiny unit-test doubles without an ASGI scope retain a
    safe compatibility path that treats their explicit client as the peer.
    """
    scope = getattr(request, "scope", None)
    if scope is None:
        return _test_double_peer(request)
    return _stashed_raw_peer(scope)


def _trusted_cf_ip(request: Request) -> str | None:
    """Return a validated Cloudflare client IP when its provenance is trusted."""
    if (
        not get_jarvis_common_settings().trust_cf_connecting_ip
        or request.headers.get("X-Jarvis-CF-Ingress") != "1"
    ):
        return None
    cf_ip = request.headers.get("CF-Connecting-IP")
    if not cf_ip:
        return None
    candidate = cf_ip.strip()
    if _parse_ip(candidate) is not None:
        return candidate
    # malformed → fall through to XFF validation below. Log: with trust opted
    # in, a malformed value is an operational anomaly (misconfigured proxy or a
    # forging attempt) worth surfacing.
    logger.warning(
        "Malformed CF-Connecting-IP header ignored (not a single IP): %r",
        candidate,
    )
    return None


def _xff_client_ip(request: Request, fallback: str) -> str:
    """Return the first untrusted XFF hop, or the authoritative fallback."""
    xff = request.headers.get("X-Forwarded-For")
    if not xff:
        return fallback
    hops = [hop.strip() for hop in xff.split(",") if hop.strip()]
    for hop in reversed(hops):
        ip = _parse_ip(hop)
        if ip is None:
            # Malformed hop — attacker-controlled string; do NOT use it as a
            # rate-limit key (distinct forged strings would let one source bypass
            # per-IP limits). Fall back to the socket peer instead.
            return fallback
        if not _is_trusted(ip):
            return hop
    # All hops were trusted proxies — fall back to socket peer.
    return fallback


def _real_ip(request: Request) -> str:
    """Return a client IP without trusting headers from an untrusted transport.

    Algorithm:
    1. Parse the stashed raw socket peer. Missing or malformed production stash
       data returns ``unknown`` instead of falling back to a rewritten client.
    2. If the raw peer is not trusted, ignore all forwarding headers and use it.
    3. Use CF-Connecting-IP only when Cloudflare trust is enabled and nginx
       marked ingress from that trusted peer.
    4. Else walk X-Forwarded-For right-to-left, skipping contiguous trusted proxies
       at the tail.  Return the first non-trusted entry (the real client).
       (A left-to-right walk let a LAN attacker prepend a fake IP and
        bypass rate limiting; right-to-left is immune to that spoofing.)
    5. If the header is absent, malformed, or contains only trusted hops, use
       the authoritative transport peer.
    """
    transport_peer = _transport_peer(request)
    if transport_peer is None:
        return "unknown"

    transport_peer_text = str(transport_peer)
    if not _is_trusted(transport_peer):
        return transport_peer_text

    cf_ip = _trusted_cf_ip(request)
    if cf_ip is not None:
        return cf_ip
    return _xff_client_ip(request, transport_peer_text)


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

    RFC 6585, section 4 requires Retry-After header on 429 responses to indicate
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
