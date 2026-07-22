"""Verify Langfuse startup configuration and warning behavior.

The startup hook leaves tracing disabled when observability is off or credentials
are incomplete. With complete configuration it constructs the client and its
quarantine-aware exporter. Configuration failures remain non-fatal.
"""

from __future__ import annotations

import logging

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

    The constructed arguments must include ``base_url``, plain-string keys,
    and the quarantine-aware span exporter.
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

    assert seen.get("base_url") == "http://langfuse.test"
    assert seen.get("public_key") == "pk-real"
    assert seen.get("secret_key") == "sk-real"
    assert isinstance(seen.get("span_exporter"), lc._QuarantineAwareSpanExporter)


def test_no_per_call_warning_flood_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """After _langfuse_lifespan_hook runs without keys, @observe calls must not each emit a WARNING.

    Calling a @observe-decorated function three times should produce at most one
    warning-level log from the 'langfuse' logger — the startup notice — not one
    per call.
    """
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

    # Run the lifespan hook (which should suppress subsequent per-call warnings).
    lc._langfuse_lifespan_hook()

    # Capture warnings from the 'langfuse' logger after the hook ran.
    langfuse_logger = logging.getLogger("langfuse")
    warnings_after_hook: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                warnings_after_hook.append(record.getMessage())

    handler = _Capture()
    langfuse_logger.addHandler(handler)
    try:
        # Import observe — it may be the real langfuse one or the no-op fallback.
        try:
            from langfuse.decorators import observe as lf_observe  # type: ignore[import-not-found]
        except ImportError:
            from langfuse import observe as lf_observe  # type: ignore[no-redef]

        @lf_observe()
        def _dummy() -> int:
            return 42

        _dummy()
        _dummy()
        _dummy()
    finally:
        langfuse_logger.removeHandler(handler)

    # At most one warning is acceptable (the startup notice already fired before
    # the handler was attached); zero is also fine.  Multiple means the flood is
    # not suppressed.
    assert len(warnings_after_hook) <= 1, (
        f"Expected at most 1 warning from langfuse logger after hook, "
        f"got {len(warnings_after_hook)}: {warnings_after_hook}"
    )
