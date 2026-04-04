"""Rate limiting shared across JARVIS services."""

import ipaddress

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

_TRUSTED_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),   # Docker default bridge
    ipaddress.ip_network("192.168.0.0/16"),
)


def _in_trusted_nets(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _TRUSTED_NETS)
    except ValueError:
        return False


def _real_ip(request: Request) -> str:
    """Return actual client IP, using rightmost XFF entry only when direct connection is trusted."""
    direct_ip = (request.client.host if request.client else None) or "unknown"
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for and _in_trusted_nets(direct_ip):
        ips = [ip.strip() for ip in forwarded_for.split(",")]
        candidate = ips[-1]  # rightmost = IP our reverse proxy appended
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass  # fall through to direct_ip
    return direct_ip


def create_limiter() -> Limiter:
    """Create a rate limiter using real client IP as key."""
    return Limiter(key_func=_real_ip)


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Handle rate limit exceeded errors with a JSON 429 response."""
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please try again later."},
    )
