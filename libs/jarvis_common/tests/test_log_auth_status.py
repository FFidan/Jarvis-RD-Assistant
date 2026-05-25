"""Unit test for app_factory._log_auth_status WARNING on CIDR refresh failure (W4-CF13)."""

import logging
from types import SimpleNamespace

import jarvis_common.app_factory as af


def test_log_auth_status_warns_when_refresh_allowed_networks_cache_raises(monkeypatch, caplog):
    def fake_raise() -> None:
        raise ValueError("invalid CIDR in test")

    monkeypatch.setattr(af, "refresh_allowed_networks_cache", fake_raise)
    monkeypatch.setattr(af, "refresh_api_key_cache", lambda: None)
    monkeypatch.setattr(af, "get_core_settings", lambda: SimpleNamespace(dev_mode=True))
    monkeypatch.setattr(af, "get_secrets_settings", lambda: SimpleNamespace(jarvis_api_key=None))

    with caplog.at_level(logging.WARNING, logger="jarvis_common.app_factory"):
        af._log_auth_status()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("refresh_allowed_networks_cache" in m for m in msgs), (
        f"Expected WARNING about refresh_allowed_networks_cache failure; got {msgs}"
    )
