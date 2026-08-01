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
from jarvis_common.net import _MAX_RETRY_AFTER_S, _reject_non_public_host, parse_retry_after

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
