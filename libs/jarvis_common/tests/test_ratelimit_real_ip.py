"""Tests for _real_ip() — right-to-left XFF walk + gated CF-Connecting-IP logic.

SEC-001: XFF must be walked right-to-left so a LAN attacker cannot bypass
         rate limiting by prepending a fake IP.
SEC-006: CF-Connecting-IP is only trusted when JARVIS_TRUST_CF_CONNECTING_IP=true.
WS-RATE-LIMIT: _user_or_ip_key returns ``user:<id>`` for authenticated callers
               and ``ip:<addr>`` for unauthenticated ones.
"""

from __future__ import annotations

import ipaddress
from unittest.mock import MagicMock

import jarvis_common.http_rate_limiter as ratelimit_mod
import pytest
from jarvis_common.http_rate_limiter import _real_ip, _user_or_ip_key
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_request(
    xff: str | None = None,
    cf: str | None = None,
    client_host: str = "127.0.0.1",
) -> Request:
    """Build a minimal mock Request with controllable headers and client."""
    req = MagicMock(spec=Request)
    req.client = MagicMock()
    req.client.host = client_host

    # Build a lowercase-keyed dict and wrap it in a MagicMock so that
    # req.headers.get() behaves like a case-insensitive HTTP headers store.
    raw: dict[str, str] = {}
    if xff is not None:
        raw["x-forwarded-for"] = xff
    if cf is not None:
        raw["cf-connecting-ip"] = cf

    headers_mock = MagicMock()
    headers_mock.get = lambda key, default=None: raw.get(key.lower(), default)
    req.headers = headers_mock

    return req


# ---------------------------------------------------------------------------
# Basic / no-XFF tests
# ---------------------------------------------------------------------------


def test_empty_xff_falls_back_to_client_host():
    """No XFF header → return request.client.host."""
    req = make_request(client_host="203.0.113.99")
    assert _real_ip(req) == "203.0.113.99"


def test_single_ip_no_trusted_proxies(monkeypatch: pytest.MonkeyPatch):
    """XFF with a single public IP, empty trusted-proxy list → return that IP."""
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req = make_request(xff="1.2.3.4")
    assert _real_ip(req) == "1.2.3.4"


# ---------------------------------------------------------------------------
# SEC-001: right-to-left XFF walk
# ---------------------------------------------------------------------------


def test_single_trusted_tail(monkeypatch: pytest.MonkeyPatch):
    """XFF='203.0.113.5, 172.18.0.1'; 172.18.0.0/12 trusted → return 203.0.113.5."""
    monkeypatch.setattr(
        ratelimit_mod,
        "_TRUSTED_PROXIES",
        [ipaddress.ip_network("172.16.0.0/12")],
    )
    req = make_request(xff="203.0.113.5, 172.18.0.1")
    assert _real_ip(req) == "203.0.113.5"


def test_multi_hop_trusted_tail(monkeypatch: pytest.MonkeyPatch):
    """XFF='203.0.113.5, 10.0.0.1, 172.18.0.1'; both internal CIDRs trusted → 203.0.113.5."""
    monkeypatch.setattr(
        ratelimit_mod,
        "_TRUSTED_PROXIES",
        [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
        ],
    )
    req = make_request(xff="203.0.113.5, 10.0.0.1, 172.18.0.1")
    assert _real_ip(req) == "203.0.113.5"


def test_attacker_spoof_blocked(monkeypatch: pytest.MonkeyPatch):
    """SEC-001 critical: attacker prepends fake '1.2.3.4' before the real client.

    XFF='1.2.3.4, 203.0.113.5, 172.18.0.1', trusted=172.18.0.0/12.
    Right-to-left walk: skip 172.18.0.1 (trusted), stop at 203.0.113.5 (untrusted).
    The spoofed prefix 1.2.3.4 is never reached → attacker cannot bypass rate limits.
    """
    monkeypatch.setattr(
        ratelimit_mod,
        "_TRUSTED_PROXIES",
        [ipaddress.ip_network("172.16.0.0/12")],
    )
    req = make_request(xff="1.2.3.4, 203.0.113.5, 172.18.0.1")
    # Must be 203.0.113.5 (real origin), NOT 1.2.3.4 (attacker's spoof)
    assert _real_ip(req) == "203.0.113.5"


def test_all_trusted_returns_client_host(monkeypatch: pytest.MonkeyPatch):
    """All XFF entries are trusted → fall back to request.client.host (socket peer)."""
    monkeypatch.setattr(
        ratelimit_mod,
        "_TRUSTED_PROXIES",
        [ipaddress.ip_network("172.16.0.0/12")],
    )
    req = make_request(xff="172.18.0.1, 172.18.0.2", client_host="172.18.0.3")
    assert _real_ip(req) == "172.18.0.3"


# ---------------------------------------------------------------------------
# Malformed entries
# ---------------------------------------------------------------------------


def test_malformed_ip_in_xff_treated_as_untrusted(monkeypatch: pytest.MonkeyPatch):
    """Malformed hop encountered during right-to-left walk → treated as untrusted client.

    XFF='not-an-ip, 172.18.0.1', trusted=172.18.0.0/12.
    Walk: skip 172.18.0.1 (trusted), hit 'not-an-ip' (malformed) → return it.
    """
    monkeypatch.setattr(
        ratelimit_mod,
        "_TRUSTED_PROXIES",
        [ipaddress.ip_network("172.16.0.0/12")],
    )
    req = make_request(xff="not-an-ip, 172.18.0.1")
    assert _real_ip(req) == "not-an-ip"


# ---------------------------------------------------------------------------
# SEC-006: CF-Connecting-IP gate
# ---------------------------------------------------------------------------


def test_cf_connecting_ip_ignored_by_default(monkeypatch: pytest.MonkeyPatch):
    """SEC-006: CF-Connecting-IP header is ignored unless JARVIS_TRUST_CF_CONNECTING_IP=true."""
    monkeypatch.delenv("JARVIS_TRUST_CF_CONNECTING_IP", raising=False)
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req = make_request(cf="9.9.9.9", client_host="127.0.0.1")
    # Header must be ignored; no XFF → fall back to client.host
    assert _real_ip(req) == "127.0.0.1"


def test_cf_connecting_ip_honoured_when_gate_enabled(monkeypatch: pytest.MonkeyPatch):
    """SEC-006: CF-Connecting-IP is used when JARVIS_TRUST_CF_CONNECTING_IP=true."""
    monkeypatch.setenv("JARVIS_TRUST_CF_CONNECTING_IP", "true")
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req = make_request(cf="9.9.9.9", client_host="127.0.0.1")
    assert _real_ip(req) == "9.9.9.9"


# ---------------------------------------------------------------------------
# WS-RATE-LIMIT: _user_or_ip_key — per-user / per-IP bucket selection
# ---------------------------------------------------------------------------


def make_request_with_state(
    user_id: int | None = None,
    xff: str | None = None,
    client_host: str = "203.0.113.1",
) -> Request:
    """Build a mock Request that also carries request.state attributes."""
    req = make_request(xff=xff, client_host=client_host)
    state = MagicMock()
    # Simulate the presence or absence of user_id on request.state.
    if user_id is not None:
        state.user_id = user_id
        # getattr(state, 'user_id', None) must return the integer directly.
        # MagicMock auto-returns a MagicMock for unknown attrs; we override:
    else:
        # Ensure getattr returns None (not a MagicMock) when user_id absent.
        del state.user_id  # remove default auto-attr so getattr returns None
    req.state = state
    return req


def test_authenticated_request_uses_user_key(monkeypatch: pytest.MonkeyPatch):
    """WS-RATE-LIMIT (a): authenticated caller gets ``user:<id>`` bucket."""
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req = make_request_with_state(user_id=42, client_host="1.2.3.4")
    assert _user_or_ip_key(req) == "user:42"


def test_two_authenticated_users_get_independent_keys(monkeypatch: pytest.MonkeyPatch):
    """WS-RATE-LIMIT (a): different user_ids produce distinct ``user:`` keys."""
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req_a = make_request_with_state(user_id=1, client_host="10.0.0.1")
    req_b = make_request_with_state(user_id=2, client_host="10.0.0.1")
    key_a = _user_or_ip_key(req_a)
    key_b = _user_or_ip_key(req_b)
    assert key_a == "user:1"
    assert key_b == "user:2"
    assert key_a != key_b


def test_unauthenticated_request_falls_back_to_ip_key(monkeypatch: pytest.MonkeyPatch):
    """WS-RATE-LIMIT (b): no session → key falls back to ``ip:<addr>``."""
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req = make_request_with_state(user_id=None, client_host="203.0.113.55")
    assert _user_or_ip_key(req) == "ip:203.0.113.55"


def test_auth_endpoint_sessionless_request_is_ip_keyed(monkeypatch: pytest.MonkeyPatch):
    """WS-RATE-LIMIT (c): /api/auth/*-style request (no session) is ip-keyed.

    Auth endpoints have no session cookie yet; SessionMiddleware leaves
    request.state.user_id unset.  The key func must fall back to IP so
    anti-enumeration limits still apply per-IP.
    """
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    # Simulate a request with no session (no user_id on state)
    req = make_request(client_host="198.51.100.7")
    # make_request returns a MagicMock; its .state is also a MagicMock where
    # getattr(req.state, 'user_id', None) returns a MagicMock (not an int).
    # Patch state so getattr returns None explicitly.
    state = MagicMock()
    del state.user_id
    req.state = state
    key = _user_or_ip_key(req)
    assert key.startswith("ip:")
    assert "198.51.100.7" in key
