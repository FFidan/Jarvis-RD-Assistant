"""Tests for _langfuse_lifespan_hook — OBSERVABILITY_ENABLED gate (DOM-J-02).

Covers three gate behaviours of :func:`jarvis_common.llm_client._langfuse_lifespan_hook`:
1. No-op when OBSERVABILITY_ENABLED is false (the default).
2. No-op when enabled but host/keys are missing.
3. Constructs Langfuse when all three (host, pk, sk) are present and enabled.

The hook is the FIRST task in app startup, runs before DB migrations, and must
NEVER raise regardless of configuration state.
"""

from __future__ import annotations

import jarvis_common.llm_client as lc
import pytest
from jarvis_common.config import JarvisCommonSettings
from jarvis_common.settings import SecretsSettings
from pydantic import SecretStr

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _common_settings(**kwargs) -> JarvisCommonSettings:
    """Build a JarvisCommonSettings with test values, bypassing env reads."""
    return JarvisCommonSettings.model_construct(**kwargs)


def _secrets_settings(**kwargs) -> SecretsSettings:
    """Build a SecretsSettings with test values, bypassing env reads and _FILE."""
    return SecretsSettings.model_construct(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook must be a no-op when observability_enabled is False (the default).

    Langfuse must not be constructed; the hook must return cleanly.
    """
    monkeypatch.setattr(
        "langfuse.Langfuse",
        lambda **k: pytest.fail("must not construct Langfuse when disabled"),
    )
    # Gate: observability_enabled=False (default). Keys present in secrets to
    # confirm the gate is checked before key resolution, not after.
    monkeypatch.setattr(
        "jarvis_common.llm_client.get_jarvis_common_settings",
        lambda: _common_settings(
            observability_enabled=False,
            langfuse_host="http://langfuse.test",
        ),
    )
    monkeypatch.setattr(
        "jarvis_common.llm_client.get_secrets_settings",
        lambda: _secrets_settings(
            langfuse_public_key=SecretStr("pk-test"),
            langfuse_secret_key=SecretStr("sk-test"),
        ),
    )

    lc._langfuse_lifespan_hook()  # must return without raising or constructing Langfuse


def test_noop_when_enabled_but_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook must be a no-op when observability_enabled is True but keys are absent.

    Partially configured state (host set, pk/sk None) must not raise and must
    not construct Langfuse (which would KeyError on missing .get_secret_value()).
    """
    monkeypatch.setattr(
        "langfuse.Langfuse",
        lambda **k: pytest.fail("must not construct Langfuse when keys absent"),
    )
    monkeypatch.setattr(
        "jarvis_common.llm_client.get_jarvis_common_settings",
        lambda: _common_settings(
            observability_enabled=True,
            langfuse_host="http://langfuse.test",
        ),
    )
    monkeypatch.setattr(
        "jarvis_common.llm_client.get_secrets_settings",
        lambda: _secrets_settings(
            langfuse_public_key=None,
            langfuse_secret_key=None,
        ),
    )

    lc._langfuse_lifespan_hook()  # must return without raising or constructing Langfuse


def test_constructs_when_enabled_and_keys_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook must construct Langfuse when enabled and all three credentials are set.

    The constructed kwargs must include host, public_key (plain str), and
    secret_key (plain str) — no SecretStr objects passed to Langfuse.
    Keys flow via SecretsSettings (_FILE-aware path), not JarvisCommonSettings.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "langfuse.Langfuse",
        lambda **k: seen.update(k),
    )
    monkeypatch.setattr(
        "jarvis_common.llm_client.get_jarvis_common_settings",
        lambda: _common_settings(
            observability_enabled=True,
            langfuse_host="http://langfuse.test",
        ),
    )
    monkeypatch.setattr(
        "jarvis_common.llm_client.get_secrets_settings",
        lambda: _secrets_settings(
            langfuse_public_key=SecretStr("pk-real"),
            langfuse_secret_key=SecretStr("sk-real"),
        ),
    )

    lc._langfuse_lifespan_hook()

    assert seen.get("host") == "http://langfuse.test"
    assert seen.get("public_key") == "pk-real"
    assert seen.get("secret_key") == "sk-real"
