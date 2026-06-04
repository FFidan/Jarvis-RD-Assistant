"""Network-safety helpers shared across JARVIS services.

All helpers are stdlib-only (``asyncio``, ``ipaddress``) so ``jarvis_common``
remains dependency-free at the network layer.
"""

from __future__ import annotations

import asyncio
import ipaddress

__all__ = ["_reject_non_public_host"]


async def _reject_non_public_host(host: str) -> None:
    """Resolve *host*; raise ``ValueError`` if it resolves to a non-public address.

    Rejects private, loopback, link-local, reserved, multicast, and unspecified
    addresses (SSRF guard).  The caller decides whether to apply the gate (e.g.
    gated by ``allow_private_smtp_host``); this helper only enforces the rule.

    Parameters
    ----------
    host:
        Hostname or IP string supplied by the operator / user.

    Raises
    ------
    ValueError
        If the host cannot be resolved (``OSError``) or if any resolved address
        is non-public.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError("Could not resolve SMTP host") from exc
    for info in infos:
        # getaddrinfo 5-tuple: (family, type, proto, canonname, sockaddr)
        sockaddr = info[4]
        # sockaddr[0] is the address string for both AF_INET and AF_INET6
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("SMTP host resolves to a non-public address")
