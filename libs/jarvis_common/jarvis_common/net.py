"""Network-safety helpers shared across JARVIS services.

All helpers are stdlib-only (``asyncio``, ``ipaddress``, ``email.utils``,
``datetime``) so ``jarvis_common`` remains dependency-free at the network layer.
"""

from __future__ import annotations

import asyncio
import ipaddress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = ["_reject_non_public_host", "parse_retry_after"]

# Default ceiling for a parsed Retry-After delay: one hour.  Caps absurdly large
# header values (e.g. ``Retry-After: 99999999999``) so a poller never blocks for
# more than an hour due to a misbehaving upstream.  Call sites that want a
# tighter ceiling (the Zotero client uses 60 s) pass ``max_seconds`` explicitly;
# arXiv passes ``max_seconds=None`` and applies its own cap downstream.
_MAX_RETRY_AFTER_S: int = 3600


def parse_retry_after(
    value: str | None,
    *,
    max_seconds: int | None = _MAX_RETRY_AFTER_S,
    negative_as_none: bool = False,
) -> int | None:
    """Parse an HTTP ``Retry-After`` header value into a whole number of seconds.

    Handles both RFC 7231 §7.1.3 forms:

    * **delta-seconds** — a non-negative number of seconds (e.g. ``"120"``).
    * **HTTP-date** — an absolute timestamp (e.g.
      ``"Wed, 21 Oct 2026 07:28:00 GMT"``); converted to the remaining delay
      relative to ``now`` (UTC), clamped to ``>= 0``.

    Parameters
    ----------
    value:
        The raw ``Retry-After`` header value, or ``None`` when absent.
    max_seconds:
        Upper bound applied to the parsed delay.  ``None`` disables capping
        (the caller is then responsible for any ceiling).  Defaults to
        :data:`_MAX_RETRY_AFTER_S` (3600 s).
    negative_as_none:
        When ``True``, a negative delta-seconds value yields ``None`` instead
        of being clamped to ``0`` (the Zotero client relies on this).

    Returns
    -------
    int | None
        The delay in whole seconds (``>= 0``, capped at ``max_seconds`` when
        set), or ``None`` when *value* is absent/empty/unparseable.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    seconds: float | None = None
    # Form 1: delta-seconds.
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = None

    if seconds is not None:
        if seconds < 0:
            return None if negative_as_none else 0
    else:
        # Form 2: HTTP-date.
        try:
            retry_dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_dt is None:
            return None
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=UTC)
        seconds = max(0.0, (retry_dt - datetime.now(tz=UTC)).total_seconds())

    result = int(seconds)
    if max_seconds is not None:
        result = min(result, max_seconds)
    return result


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
