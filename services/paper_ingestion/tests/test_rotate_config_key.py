"""Tests for the config-key rotation script's local validation seams."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_rotate_config_key():
    path = Path(__file__).resolve().parents[3] / "scripts" / "rotate_config_key.py"
    spec = importlib.util.spec_from_file_location("rotate_config_key_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rotate_config_key = _load_rotate_config_key()


def test_required_env_returns_present_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured environment values should pass through unchanged."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")

    assert rotate_config_key._required_env("DATABASE_URL") == "postgresql://example"


def test_required_env_exits_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing required settings should fail before any database connection."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit, match="DATABASE_URL is required"):
        rotate_config_key._required_env("DATABASE_URL")
