"""Unit tests for _ip_in_allowlist cold-start cache write-back."""

import ipaddress

import jarvis_common.auth as auth_module
import pytest


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset module-level cache before and after each test."""
    original = auth_module._CACHED_ALLOWED_NETWORKS
    auth_module._CACHED_ALLOWED_NETWORKS = None
    yield
    auth_module._CACHED_ALLOWED_NETWORKS = original


def test_cold_start_populates_cache(monkeypatch):
    """Calling _ip_in_allowlist with a cold cache must write back the parsed result."""
    monkeypatch.setattr(
        auth_module,
        "_parse_allowed_networks",
        lambda: [ipaddress.ip_network("127.0.0.1/32")],
    )
    assert auth_module._CACHED_ALLOWED_NETWORKS is None
    auth_module._ip_in_allowlist("127.0.0.1")
    assert auth_module._CACHED_ALLOWED_NETWORKS is not None
    assert any(str(n) == "127.0.0.1/32" for n in auth_module._CACHED_ALLOWED_NETWORKS)


def test_warm_cache_not_reparsed(monkeypatch):
    """After first call, _parse_allowed_networks is not called again."""
    calls = []

    def counting_parse():
        calls.append(1)
        return [ipaddress.ip_network("10.0.0.0/8")]

    monkeypatch.setattr(auth_module, "_parse_allowed_networks", counting_parse)
    auth_module._ip_in_allowlist("10.1.2.3")
    auth_module._ip_in_allowlist("10.4.5.6")
    assert len(calls) == 1, "Parser should be called once; cache reused on second call"
