"""Rate limiting shared across JARVIS services."""

import ipaddress
import os
from collections.abc import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

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

_TRUSTED_PROXIES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network(c.strip())
    for c in (os.environ.get("TRUSTED_PROXY_CIDRS", "").split(",") + _DEFAULT_PROXY_CIDRS)
    if c.strip()
]


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
    # SEC-006: CF-Connecting-IP only trusted when operator explicitly enables it.
    if os.environ.get("JARVIS_TRUST_CF_CONNECTING_IP", "").lower() == "true":
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()

    xff = request.headers.get("X-Forwarded-For")
    if not xff:
        return request.client.host if request.client else "unknown"

    # Parse and walk right-to-left (SEC-001 fix).
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            # Malformed hop — treat as untrusted, return it as the client.
            return hop
        if not _is_trusted(ip):
            return hop

    # All hops were trusted proxies — fall back to socket peer.
    return request.client.host if request.client else "unknown"


def create_limiter(default_limits: list[str | Callable[..., str]] | None = None) -> Limiter:
    """Create a rate limiter using real client IP as key.

    Parameters
    ----------
    default_limits:
        Optional list of limit strings applied as a global cap on every
        request (e.g. ``["600/minute"]``).  These are enforced by
        ``SlowAPIMiddleware`` before any route-level auth takes effect,
        providing a first-line defence against unauthenticated brute-force.
        Defaults to ``["600/minute"]`` when not specified.
    """
    if default_limits is None:
        default_limits = ["600/minute"]
    return Limiter(key_func=_real_ip, default_limits=default_limits)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Handle rate limit exceeded errors with a JSON 429 response."""
    _ = request  # Starlette requires (request, exc) interface; path not needed in 429 body
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded ({exc.limit}). Please try again later."},
    )
