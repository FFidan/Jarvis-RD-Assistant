"""Tests for jarvis_common.net._reject_non_public_host (SSRF guard).

Verifies that the helper raises ValueError for private, loopback, and
link-local addresses (literal IPs and unresolvable hosts), and does NOT raise
for a genuine public IP address.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from jarvis_common.net import _reject_non_public_host

# ---------------------------------------------------------------------------
# Rejects private / loopback / link-local literal IPs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",  # RFC-1918 private
        "169.254.169.254",  # link-local (cloud metadata endpoint)
        "127.0.0.1",  # loopback
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
