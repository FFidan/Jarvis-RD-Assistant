"""Tests for CFG-CIDR-1: TRUSTED_PROXY_CIDRS env var overrides (not extends) defaults.

Acceptance criteria:
- When TRUSTED_PROXY_CIDRS is set, it is used exclusively (no RFC-1918 bleed-through).
- When TRUSTED_PROXY_CIDRS is absent/empty, _DEFAULT_PROXY_CIDRS is the fallback.
"""

import importlib
import ipaddress
from dataclasses import dataclass

import pytest


@dataclass
class _FakeClient:
    """Socket-peer stand-in: ``_real_ip`` reads only ``.host``."""

    host: str


@dataclass
class _FakeRequest:
    """Minimal Request stand-in exposing only what ``_real_ip`` reads."""

    headers: dict[str, str]
    client: _FakeClient


def _reload_module():
    """Reload http_rate_limiter (and its config dep) so module-level trusted
    proxies and the settings cache reflect the current environment."""
    import jarvis_common.config as cfg
    import jarvis_common.http_rate_limiter as m

    importlib.reload(cfg)
    importlib.reload(m)
    return m


def _reload_and_build(monkeypatch: pytest.MonkeyPatch) -> list:
    """Reload http_rate_limiter after env mutation and return fresh proxy list."""
    return _reload_module()._build_trusted_proxies()


def test_trusted_proxy_cidrs_env_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TRUSTED_PROXY_CIDRS is set, it must override (not extend) defaults.

    Verifies that 172.16.0.0/12 (part of RFC-1918 defaults) is NOT in the
    trusted proxy list when TRUSTED_PROXY_CIDRS=10.137.241.0/24.
    """
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.137.241.0/24")
    proxies = _reload_and_build(monkeypatch)

    broad_default = ipaddress.ip_network("172.16.0.0/12")
    assert broad_default not in proxies, (
        "Broad RFC-1918 default 172.16.0.0/12 must not be trusted when "
        "TRUSTED_PROXY_CIDRS is explicitly set by the operator"
    )

    # The configured CIDR must be present
    expected = ipaddress.ip_network("10.137.241.0/24")
    assert expected in proxies, "TRUSTED_PROXY_CIDRS value must appear in trusted proxies"


def test_trusted_proxy_cidrs_env_absent_uses_loopback_only_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TRUSTED_PROXY_CIDRS is absent, the code default trusts loopback only."""
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)
    proxies = _reload_and_build(monkeypatch)

    assert ipaddress.ip_network("127.0.0.0/8") in proxies, (
        "loopback must be trusted by default when the env var is absent"
    )
    # The broad RFC-1918 ranges must NOT be trusted by default — otherwise any
    # container on a Docker bridge could spoof X-Forwarded-For.
    for broad in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]:
        assert ipaddress.ip_network(broad) not in proxies, (
            f"broad RFC-1918 range {broad} must not be trusted by default"
        )


def test_trusted_proxy_cidrs_env_empty_uses_loopback_only_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty TRUSTED_PROXY_CIDRS (e.g. TRUSTED_PROXY_CIDRS='') falls back to loopback only."""
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "")
    proxies = _reload_and_build(monkeypatch)

    assert ipaddress.ip_network("127.0.0.0/8") in proxies
    assert ipaddress.ip_network("172.16.0.0/12") not in proxies, (
        "empty TRUSTED_PROXY_CIDRS must fall back to loopback only, not the RFC-1918 ranges"
    )


def test_trusted_proxy_cidrs_multiple_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple comma-separated CIDRs in TRUSTED_PROXY_CIDRS are all applied exclusively."""
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.137.241.0/24,127.0.0.1/32")
    proxies = _reload_and_build(monkeypatch)

    assert ipaddress.ip_network("10.137.241.0/24") in proxies
    assert ipaddress.ip_network("127.0.0.1/32") in proxies
    # RFC-1918 defaults must not bleed in
    assert ipaddress.ip_network("172.16.0.0/12") not in proxies
    assert ipaddress.ip_network("192.168.0.0/16") not in proxies


def test_spoofed_xff_from_nonloopback_peer_keys_off_real_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback-only default: a forged X-Forwarded-For cannot move the rate-limit key.

    An attacker connecting from a non-loopback RFC-1918 address (10.5.5.5) crafts
    ``X-Forwarded-For: 1.2.3.4, 10.5.5.5`` to claim 1.2.3.4 is the upstream client.
    Its own hop is NOT a trusted proxy under the loopback-only default, so the
    right-to-left walk stops there: the limiter keys off the real peer, never the
    spoofed 1.2.3.4.
    """
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)
    m = _reload_module()

    req = _FakeRequest(
        headers={"X-Forwarded-For": "1.2.3.4, 10.5.5.5"},
        client=_FakeClient("10.5.5.5"),
    )
    assert m._real_ip(req) == "10.5.5.5", (
        "a non-loopback peer's forged XFF must not be trusted under the loopback-only default"
    )


def test_configured_bridge_hop_is_trusted_for_per_client_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the bridge subnet configured, the real nginx hop is trusted and the
    upstream client (left of the trusted hop) keys the limiter — per-client bucketing.
    """
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.0/8,10.137.241.0/24")
    m = _reload_module()

    req = _FakeRequest(
        headers={"X-Forwarded-For": "203.0.113.9, 10.137.241.2"},
        client=_FakeClient("10.137.241.2"),
    )
    assert m._real_ip(req) == "203.0.113.9", (
        "the real client must be extracted when the configured bridge hop is trusted"
    )
