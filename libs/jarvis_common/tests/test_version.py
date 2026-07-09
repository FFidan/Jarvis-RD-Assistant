"""Resolution-order tests for ``jarvis_common.version.app_version``.

The service images install only the ``jarvis_common`` wheel plus copied source,
so the ``jarvis-rd-assistant`` distribution is NOT discoverable in-container and
``importlib.metadata.version`` raises there. These tests pin the effective
production path — the ``JARVIS_VERSION`` env fallback — which a self-referential
``app.version == app_version()`` assertion cannot catch (both would be "unknown").
"""

from __future__ import annotations

import importlib.metadata

from jarvis_common.version import app_version


def _dist_absent(_name: str) -> str:
    raise importlib.metadata.PackageNotFoundError(_name)


def test_prefers_installed_distribution(monkeypatch):
    # When the root dist is discoverable its metadata wins over the env var.
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "9.9.9")
    monkeypatch.setenv("JARVIS_VERSION", "1.0.4")
    assert app_version() == "9.9.9"


def test_falls_back_to_jarvis_version_env_in_container(monkeypatch):
    # The container condition: root dist absent -> the deployment's JARVIS_VERSION.
    monkeypatch.setattr("importlib.metadata.version", _dist_absent)
    monkeypatch.setenv("JARVIS_VERSION", "1.0.4")
    assert app_version() == "1.0.4"


def test_unknown_when_neither_available(monkeypatch):
    # A bare source checkout with no env: honest "unknown", never a crash.
    monkeypatch.setattr("importlib.metadata.version", _dist_absent)
    monkeypatch.delenv("JARVIS_VERSION", raising=False)
    assert app_version() == "unknown"
