"""Rate limiting shared across JARVIS services."""

import ipaddress
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

# Trusted proxy CIDRs loaded once at import time.
# Override / extend via TRUSTED_PROXY_CIDRS env var (comma-separated CIDRs).
# Defaults include Docker bridge / RFC-1918 ranges so Docker-internal hops are
# always skipped when walking X-Forwarded-For left-to-right.
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


def _real_ip(request: Request) -> str:
    """Return actual client IP via CF-Connecting-IP or XFF walk-left past trusted proxies."""
    # 1. Cloudflare tunnel: CF-Connecting-IP is not forgeable when the tunnel
    #    is the only ingress path — prefer it unconditionally.
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    # 2. Walk XFF left-to-right, return first non-trusted entry.
    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return request.client.host if request.client else "unknown"
    ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue  # skip malformed entries
        if not any(addr in net for net in _TRUSTED_PROXIES):
            return ip
    # All entries were trusted proxies (e.g. single-hop behind Docker bridge).
    return ips[-1] if ips else (request.client.host if request.client else "unknown")


def create_limiter() -> Limiter:
    """Create a rate limiter using real client IP as key."""
    return Limiter(key_func=_real_ip)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Handle rate limit exceeded errors with a JSON 429 response."""
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please try again later."},
    )
