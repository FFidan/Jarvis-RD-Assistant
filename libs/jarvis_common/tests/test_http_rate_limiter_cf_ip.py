"""Tests for CF-Connecting-IP validation in http_rate_limiter._real_ip.

SEC-006 follow-up: when ``JARVIS_TRUST_CF_CONNECTING_IP`` is enabled the
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
    """A single well-formed CF-Connecting-IP is still returned verbatim."""
    req = _FakeRequest(headers={"CF-Connecting-IP": "198.51.100.7"})

    assert cf_trust_enabled._real_ip(req) == "198.51.100.7"


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
            # XFF: 8.8.8.8 (public client) -> 10.0.0.5 (trusted Docker hop).
            "X-Forwarded-For": "8.8.8.8, 10.0.0.5",
        }
    )

    # Falls through to XFF; right-to-left walk skips the trusted 10/8 hop and
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
    """COM-SEC-001: a malformed XFF hop must yield the socket peer, not the forged string.

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
            headers={"X-Forwarded-For": "not-an-ip, 10.0.0.1"},
            client=_FakeClient(host="203.0.113.55"),
        )
        result = _m._real_ip(req)
    finally:
        _m.get_jarvis_common_settings = original

    assert result == "203.0.113.55", (
        f"Malformed XFF hop must yield the socket peer '203.0.113.55', got {result!r}"
    )


def test_malformed_xff_hop_with_no_client_returns_unknown() -> None:
    """COM-SEC-001: malformed XFF hop with no socket peer returns 'unknown'."""
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
        headers={"CF-Connecting-IP": "not-an-ip"},
        client=_FakeClient(host="203.0.113.42"),
    )

    # No XFF header → after the bad CF value is rejected, fall to request.client.host.
    with caplog.at_level("WARNING", logger=cf_trust_enabled.logger.name):
        assert cf_trust_enabled._real_ip(req) == "203.0.113.42"

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING" and "Malformed CF-Connecting-IP" in rec.getMessage()
    ]
    assert len(warnings) == 1, (
        "a garbage CF-Connecting-IP must be logged at WARNING before falling through; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )
