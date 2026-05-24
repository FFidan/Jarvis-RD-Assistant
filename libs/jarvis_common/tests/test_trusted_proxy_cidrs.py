"""Tests for CFG-CIDR-1: TRUSTED_PROXY_CIDRS env var overrides (not extends) defaults.

Acceptance criteria:
- When TRUSTED_PROXY_CIDRS is set, it is used exclusively (no RFC-1918 bleed-through).
- When TRUSTED_PROXY_CIDRS is absent/empty, _DEFAULT_PROXY_CIDRS is the fallback.
"""

import importlib
import ipaddress

import pytest


def _reload_and_build(monkeypatch: pytest.MonkeyPatch) -> list:
    """Reload http_rate_limiter after env mutation and return fresh proxy list."""
    # Force settings cache to refresh too
    import jarvis_common.config as cfg
    import jarvis_common.http_rate_limiter as m

    importlib.reload(cfg)
    importlib.reload(m)
    return m._build_trusted_proxies()


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


def test_trusted_proxy_cidrs_env_absent_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TRUSTED_PROXY_CIDRS is absent/empty, _DEFAULT_PROXY_CIDRS is used."""
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)
    proxies = _reload_and_build(monkeypatch)

    # All built-in defaults should be present
    for cidr_str in ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]:
        assert ipaddress.ip_network(cidr_str) in proxies, (
            f"Default CIDR {cidr_str} must be in trusted proxies when env var is absent"
        )


def test_trusted_proxy_cidrs_env_empty_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty TRUSTED_PROXY_CIDRS (e.g. TRUSTED_PROXY_CIDRS='') falls back to defaults."""
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "")
    proxies = _reload_and_build(monkeypatch)

    assert ipaddress.ip_network("172.16.0.0/12") in proxies, (
        "Empty TRUSTED_PROXY_CIDRS must fall back to defaults (including 172.16.0.0/12)"
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
