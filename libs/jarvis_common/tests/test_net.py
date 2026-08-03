"""Tests for jarvis_common.net._reject_non_public_host (SSRF guard).

Verifies that the helper raises ValueError for private, loopback, link-local
and CGNAT addresses (literal IPs and unresolvable hosts), and does NOT raise
for a genuine public IP address.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import pytest
from jarvis_common.net import (
    _MAX_RETRY_AFTER_S,
    _reject_non_public_host,
    is_non_public_address,
    parse_retry_after,
)

# ---------------------------------------------------------------------------
# Rejects private / loopback / link-local literal IPs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",  # RFC-1918 private
        "169.254.169.254",  # link-local (cloud metadata endpoint)
        "127.0.0.1",  # loopback
        "100.64.0.1",  # RFC-6598 CGNAT — not covered by is_private
    ],
)
async def test_rejects_private_and_special_ips(host: str) -> None:
    """Literal private/loopback/link-local IPs must raise ValueError."""
    with pytest.raises(ValueError, match="non-public"):
        await _reject_non_public_host(host)


# ---------------------------------------------------------------------------
# Allows a public IP
# ---------------------------------------------------------------------------


async def test_allows_public_ip() -> None:
    """A public IP (8.8.8.8) must NOT raise."""
    # 8.8.8.8 resolves to itself — public address — so no ValueError.
    await _reject_non_public_host("8.8.8.8")


def test_is_non_public_address_covers_every_class() -> None:
    """The shared predicate refuses all seven non-public classes and admits public IPs.

    CGNAT (100.64.0.0/10) and multicast are the classes the historical
    private/loopback/link-local guard missed; each is non-public ONLY through its
    own clause here, so this pins them independently of the is_private overlap that
    already subsumes reserved/unspecified on IPv4.
    """
    from ipaddress import ip_address

    non_public = [
        "10.0.0.1",  # private
        "127.0.0.1",  # loopback
        "169.254.0.1",  # link-local
        "240.0.0.1",  # reserved
        "224.0.0.1",  # multicast (v4) — only is_multicast catches this
        "0.0.0.0",  # unspecified
        "100.64.0.1",  # CGNAT — only the CGNAT clause catches this
        "::1",  # loopback (v6)
        "fe80::1",  # link-local (v6)
        "ff02::1",  # multicast (v6)
    ]
    for addr in non_public:
        assert is_non_public_address(ip_address(addr)) is True, addr

    for public in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
        assert is_non_public_address(ip_address(public)) is False, public


# ---------------------------------------------------------------------------
# Rejects on resolution failure
# ---------------------------------------------------------------------------


async def test_rejects_on_resolution_failure() -> None:
    """An unresolvable host (getaddrinfo raises OSError) must raise ValueError."""
    import asyncio

    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", new=AsyncMock(side_effect=OSError("no such host"))):
        with pytest.raises(ValueError, match="resolve"):
            await _reject_non_public_host("unresolvable.internal.example")


# ---------------------------------------------------------------------------
# parse_retry_after — canonical Retry-After parser
# ---------------------------------------------------------------------------


def test_parse_retry_after_delta_seconds() -> None:
    """The delta-seconds form is parsed to a whole-second int."""
    assert parse_retry_after("120") == 120
    assert parse_retry_after("0") == 0
    # Decimal delta-seconds truncate toward zero (matches int(float(value))).
    assert parse_retry_after("2.9") == 2


def test_parse_retry_after_http_date_form() -> None:
    """The HTTP-date form is converted to the remaining delay (clamped >= 0)."""
    future = datetime.now(tz=UTC) + timedelta(seconds=45)
    delay = parse_retry_after(format_datetime(future, usegmt=True))
    assert delay is not None
    # Allow a small scheduling slack around the 45 s target.
    assert 40 <= delay <= 45


def test_parse_retry_after_http_date_in_past_is_zero() -> None:
    """An HTTP-date already in the past clamps to 0, not a negative."""
    past = datetime.now(tz=UTC) - timedelta(hours=1)
    assert parse_retry_after(format_datetime(past, usegmt=True)) == 0


def test_parse_retry_after_caps_at_default_max() -> None:
    """A delta far above the default cap is clamped to _MAX_RETRY_AFTER_S."""
    assert parse_retry_after("99999999999") == _MAX_RETRY_AFTER_S
    assert _MAX_RETRY_AFTER_S == 3600


def test_parse_retry_after_custom_cap() -> None:
    """A tighter explicit cap (e.g. the Zotero client's 60 s) is honoured."""
    assert parse_retry_after("120", max_seconds=60) == 60
    assert parse_retry_after("30", max_seconds=60) == 30


def test_parse_retry_after_uncapped() -> None:
    """max_seconds=None disables capping (arXiv applies its own ceiling)."""
    assert parse_retry_after("99999999999", max_seconds=None) == 99999999999


def test_parse_retry_after_none_and_garbage() -> None:
    """Absent/empty/unparseable inputs return None."""
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("not-a-number-or-date") is None


def test_parse_retry_after_negative_clamps_to_zero_by_default() -> None:
    """A negative delta clamps to 0 by default (matches the arXiv parser)."""
    assert parse_retry_after("-1") == 0


def test_parse_retry_after_negative_as_none() -> None:
    """negative_as_none=True yields None for a negative delta (Zotero posture)."""
    assert parse_retry_after("-1", negative_as_none=True) is None
