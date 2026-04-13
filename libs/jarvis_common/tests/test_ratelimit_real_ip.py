"""Tests for _real_ip() — XFF walk-left + CF-Connecting-IP logic."""

from __future__ import annotations

import ipaddress
from unittest.mock import MagicMock

import jarvis_common.ratelimit as ratelimit_mod
import pytest
from jarvis_common.ratelimit import _real_ip
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
    headers_mock.get = lambda key, default="": raw.get(key.lower(), default)
    req.headers = headers_mock

    return req


# ---------------------------------------------------------------------------
# Tests
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


def test_multi_hop_trusted_proxy_skipped(monkeypatch: pytest.MonkeyPatch):
    """XFF = '203.0.113.5, 172.18.0.1'; 172.18.0.0/12 is trusted → return first."""
    monkeypatch.setattr(
        ratelimit_mod,
        "_TRUSTED_PROXIES",
        [ipaddress.ip_network("172.16.0.0/12")],
    )
    req = make_request(xff="203.0.113.5, 172.18.0.1")
    assert _real_ip(req) == "203.0.113.5"


def test_cf_connecting_ip_takes_precedence(monkeypatch: pytest.MonkeyPatch):
    """CF-Connecting-IP header wins over XFF regardless of trusted-proxy config."""
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req = make_request(xff="10.0.0.1, 10.0.0.2", cf="5.6.7.8")
    assert _real_ip(req) == "5.6.7.8"


def test_malformed_ip_in_xff_skipped(monkeypatch: pytest.MonkeyPatch):
    """Malformed entries in XFF are skipped; first valid non-trusted IP is returned."""
    monkeypatch.setattr(ratelimit_mod, "_TRUSTED_PROXIES", [])
    req = make_request(xff="not-an-ip, 1.2.3.4")
    assert _real_ip(req) == "1.2.3.4"


def test_all_trusted_returns_last(monkeypatch: pytest.MonkeyPatch):
    """All XFF entries are in a trusted CIDR → fall back to the last entry."""
    monkeypatch.setattr(
        ratelimit_mod,
        "_TRUSTED_PROXIES",
        [ipaddress.ip_network("10.0.0.0/8")],
    )
    req = make_request(xff="10.0.0.1, 10.0.0.2, 10.0.0.3")
    assert _real_ip(req) == "10.0.0.3"
