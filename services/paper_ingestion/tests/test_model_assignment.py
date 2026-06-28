from __future__ import annotations

import logging
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from paper_ingestion.services.model_assignment import reload_telegram_nudges

_BOT_URL = "http://telegram-bot-test:8002"
_RELOAD_URL = f"{_BOT_URL}/internal/reload-nudges"
_LOGGER = "paper_ingestion.services.model_assignment"


def _patch_settings(monkeypatch):
    telegram_settings = MagicMock()
    telegram_settings.url_or_none = _BOT_URL

    secrets_settings = MagicMock()
    api_key_mock = MagicMock()
    api_key_mock.get_secret_value.return_value = "testkey"
    secrets_settings.jarvis_api_key = api_key_mock

    monkeypatch.setattr(
        "paper_ingestion.services.model_assignment.get_telegram_settings",
        lambda: telegram_settings,
    )
    monkeypatch.setattr(
        "paper_ingestion.services.model_assignment.get_secrets_settings",
        lambda: secrets_settings,
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500])
async def test_http_error_triggers_warning_log(monkeypatch, caplog, status):
    _patch_settings(monkeypatch)

    respx.post(_RELOAD_URL).mock(return_value=httpx.Response(status))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await reload_telegram_nudges()

    assert caplog.records, f"Expected a warning log for HTTP {status} — none was emitted"
    assert any(r.levelname == "WARNING" for r in caplog.records)


@respx.mock
@pytest.mark.asyncio
async def test_http_200_no_warning(monkeypatch, caplog):
    _patch_settings(monkeypatch)

    respx.post(_RELOAD_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await reload_telegram_nudges()

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert not warning_records, f"Unexpected warning on 200: {warning_records}"
