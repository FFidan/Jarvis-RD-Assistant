"""Startup wiring contracts for the Telegram bot entrypoint."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_main_has_no_configuration_encryption_reload_hook() -> None:
    """Telegram startup must not import or register Fernet cache reloading."""
    import telegram_bot.main as main_module

    assert not hasattr(main_module, "reload_fernet_on_sighup")


async def test_startup_exports_traces_whenever_a_collector_endpoint_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot joins the same trace as every service it calls.

    ``observability_enabled`` is the Langfuse master gate. Reusing it here means
    the bot's spans are missing from a collector that already receives every
    other service's, breaking the trace exactly where a user request crosses
    into the bot.
    """
    import telegram_bot.main as main_module

    settings = SimpleNamespace(
        observability_enabled=False,
        otel_exporter_otlp_traces_endpoint="http://collector.test:4318/v1/traces",
        otel_export_timeout_ms=1,
    )
    configure = MagicMock(return_value=True)
    monkeypatch.setattr(main_module, "ensure_outbound_egress_allowed", lambda _label: None)
    monkeypatch.setattr(main_module, "get_jarvis_common_settings", lambda: settings)
    monkeypatch.setattr(main_module, "configure_telemetry", configure)

    # Telemetry is configured before any bot resource is built, so startup stops
    # at the first missing collaborator once the call under test has happened.
    with pytest.raises(KeyError):
        await main_module.post_init(SimpleNamespace(bot_data={}))

    kwargs = configure.call_args.kwargs
    assert kwargs["enabled"] is True
    assert kwargs["otlp_endpoint"] == "http://collector.test:4318/v1/traces"
    assert kwargs["timeout_ms"] == 1
