"""Tests for CF-Connecting-IP validation in http_rate_limiter._real_ip.

When ``JARVIS_TRUST_CF_CONNECTING_IP`` is enabled the
CF-Connecting-IP header is trusted as the canonical client IP — but only when
it is a single, well-formed IP address.  A malformed value (e.g. two
comma-separated IPs forged by an attacker, or garbage) must NOT be trusted; the
code must fall through to the validated right-to-left X-Forwarded-For walk
rather than crash or honour the bad value.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest


@dataclass
class _FakeClient:
    host: str = "203.0.113.9"


@dataclass
class _FakeRequest:
    """Minimal Request stand-in exposing only what ``_real_ip`` reads."""

    headers: dict[str, str] = field(default_factory=dict)
    client: _FakeClient | None = field(default_factory=_FakeClient)
    # ``None`` models a narrow unit-test double with no ASGI scope. Production
    # requests always expose a dict; an empty dict therefore models a missing
    # RawClientStashMiddleware snapshot and must fail closed.
    scope: dict[str, object] | None = None


@dataclass
class _Settings:
    trust_cf_connecting_ip: bool = True


@pytest.fixture
def cf_trust_enabled(monkeypatch: pytest.MonkeyPatch):
    """Enable CF-Connecting-IP trust for the duration of a test."""
    import jarvis_common.http_rate_limiter as m

    monkeypatch.setattr(m, "get_jarvis_common_settings", lambda: _Settings(True))
    return m


def test_valid_cf_connecting_ip_is_honoured(cf_trust_enabled) -> None:
    """The raw trusted peer proves provenance; CF identity keys the limit.

    This models production middleware order: RawClientStashMiddleware records
    nginx's socket address, then ProxyHeadersMiddleware rewrites
    ``request.client`` from X-Forwarded-For before SlowAPI calls ``_real_ip``.
    """
    req = _FakeRequest(
        headers={
            "CF-Connecting-IP": "198.51.100.7",
            "X-Jarvis-CF-Ingress": "1",
        },
        client=_FakeClient(host="203.0.113.80"),
        scope={"jarvis.raw_client": ("127.0.0.1", 43120)},
    )

    assert cf_trust_enabled._real_ip(req) == "198.51.100.7"


@pytest.mark.parametrize("marker", [None, "0", "true", "1, 1"])
def test_cf_header_without_exact_ingress_marker_is_ignored(cf_trust_enabled, marker) -> None:
    """Caddy, Tailscale, and raw ingress cannot promote a forged CF header."""
    headers = {"CF-Connecting-IP": "198.51.100.7"}
    if marker is not None:
        headers["X-Jarvis-CF-Ingress"] = marker
    req = _FakeRequest(
        headers=headers,
        client=_FakeClient(host="127.0.0.1"),
        scope={"jarvis.raw_client": ("127.0.0.1", 43120)},
    )

    assert cf_trust_enabled._real_ip(req) == "127.0.0.1"


def test_cf_marker_from_untrusted_raw_socket_peer_is_ignored(cf_trust_enabled) -> None:
    """A forged XFF rewrite cannot make an untrusted socket peer trusted."""
    req = _FakeRequest(
        headers={
            "CF-Connecting-IP": "198.51.100.7",
            "X-Jarvis-CF-Ingress": "1",
        },
        # ProxyHeadersMiddleware has already rewritten this to a trusted value.
        client=_FakeClient(host="127.0.0.1"),
        # RawClientStashMiddleware retained the actual sibling-container peer.
        scope={"jarvis.raw_client": ("172.31.9.9", 43120)},
    )

    assert cf_trust_enabled._real_ip(req) == "172.31.9.9"


def test_xff_from_untrusted_raw_socket_peer_is_ignored(cf_trust_enabled) -> None:
    """An untrusted transport peer cannot select a key through forwarded headers."""
    req = _FakeRequest(
        headers={"X-Forwarded-For": "198.51.100.44, 127.0.0.1"},
        # ProxyHeadersMiddleware already rewrote the visible client.
        client=_FakeClient(host="127.0.0.1"),
        # The outer middleware retained the real, untrusted transport peer.
        scope={"jarvis.raw_client": ("172.31.9.9", 43120)},
    )

    assert cf_trust_enabled._real_ip(req) == "172.31.9.9"


def test_cf_marker_without_raw_peer_stash_is_ignored(cf_trust_enabled) -> None:
    """Cloudflare provenance fails closed if the outer stash middleware is absent."""
    req = _FakeRequest(
        headers={
            "CF-Connecting-IP": "198.51.100.7",
            "X-Jarvis-CF-Ingress": "1",
        },
        client=_FakeClient(host="127.0.0.1"),
        scope={},
    )

    assert cf_trust_enabled._real_ip(req) == "unknown"


@pytest.mark.parametrize(
    "raw_peer",
    [None, (), (1234, 43120), ("not-an-ip", 43120)],
)
def test_forwarding_headers_with_malformed_raw_peer_fail_closed(
    cf_trust_enabled, raw_peer: object
) -> None:
    """Malformed production stash data cannot fall back to a rewritten client."""
    req = _FakeRequest(
        headers={"X-Forwarded-For": "198.51.100.44, 127.0.0.1"},
        client=_FakeClient(host="127.0.0.1"),
        scope={"jarvis.raw_client": raw_peer},
    )

    assert cf_trust_enabled._real_ip(req) == "unknown"


def test_malformed_cf_connecting_ip_falls_through_to_xff(
    cf_trust_enabled, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed CF-Connecting-IP (two IPs) must not crash and must not be trusted.

    The code falls through to the validated X-Forwarded-For walk, which returns
    the first non-trusted hop (the real client) from the right.  The anomaly is
    logged at WARNING so a forged / misconfigured header is observable.
    """
    req = _FakeRequest(
        headers={
            # Two IPs — invalid for a single-IP parse; an attacker could forge this.
            "CF-Connecting-IP": "1.2.3.4, 5.6.7.8",
            "X-Jarvis-CF-Ingress": "1",
            # XFF: 8.8.8.8 (public client) -> 127.0.0.1 (trusted loopback hop).
            "X-Forwarded-For": "8.8.8.8, 127.0.0.1",
        },
        client=_FakeClient(host="127.0.0.1"),
        scope={"jarvis.raw_client": ("127.0.0.1", 43120)},
    )

    # Falls through to XFF; right-to-left walk skips the trusted loopback hop and
    # returns the first non-trusted entry.
    with caplog.at_level("WARNING", logger=cf_trust_enabled.logger.name):
        assert cf_trust_enabled._real_ip(req) == "8.8.8.8"

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING" and "Malformed CF-Connecting-IP" in rec.getMessage()
    ]
    assert len(warnings) == 1, (
        "a malformed CF-Connecting-IP must be logged at WARNING before falling through; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )


def test_malformed_xff_hop_falls_back_to_socket_peer() -> None:
    """A malformed XFF hop must yield the socket peer, not the forged string.

    An attacker can inject a hop like "not-an-ip" into X-Forwarded-For to create
    distinct rate-limit keys for each request (bypassing per-IP limits).  The fix
    must return ``request.client.host`` instead of the malformed string.
    """
    import jarvis_common.http_rate_limiter as _m

    # Disable CF trust so only the XFF path is exercised.
    m_settings = _Settings(trust_cf_connecting_ip=False)

    original = _m.get_jarvis_common_settings
    _m.get_jarvis_common_settings = lambda: m_settings
    try:
        req = _FakeRequest(
            headers={"X-Forwarded-For": "not-an-ip, 127.0.0.1"},
            client=_FakeClient(host="203.0.113.55"),
        )
        result = _m._real_ip(req)
    finally:
        _m.get_jarvis_common_settings = original

    assert result == "203.0.113.55", (
        f"Malformed XFF hop must yield the socket peer '203.0.113.55', got {result!r}"
    )


def test_malformed_xff_hop_with_no_client_returns_unknown() -> None:
    """Malformed XFF hop with no socket peer returns 'unknown'."""
    import jarvis_common.http_rate_limiter as _m

    m_settings = _Settings(trust_cf_connecting_ip=False)
    original = _m.get_jarvis_common_settings
    _m.get_jarvis_common_settings = lambda: m_settings
    try:
        req = _FakeRequest(
            headers={"X-Forwarded-For": "definitely-not-an-ip"},
            client=None,
        )
        result = _m._real_ip(req)
    finally:
        _m.get_jarvis_common_settings = original

    assert result == "unknown", (
        f"Malformed XFF hop with no client must return 'unknown', got {result!r}"
    )


def test_garbage_cf_connecting_ip_falls_through_to_socket_peer(
    cf_trust_enabled, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-IP CF-Connecting-IP with no XFF falls back to the socket peer."""
    req = _FakeRequest(
        headers={"CF-Connecting-IP": "not-an-ip", "X-Jarvis-CF-Ingress": "1"},
        client=_FakeClient(host="127.0.0.1"),
        scope={"jarvis.raw_client": ("127.0.0.1", 43120)},
    )

    # No XFF header → after the bad CF value is rejected, fall to request.client.host.
    with caplog.at_level("WARNING", logger=cf_trust_enabled.logger.name):
        assert cf_trust_enabled._real_ip(req) == "127.0.0.1"

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING" and "Malformed CF-Connecting-IP" in rec.getMessage()
    ]
    assert len(warnings) == 1, (
        "a garbage CF-Connecting-IP must be logged at WARNING before falling through; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )
