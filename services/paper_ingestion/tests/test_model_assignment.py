from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from fastapi import HTTPException

from paper_ingestion.services.config_write import write_config
from paper_ingestion.services.model_assignment import reload_telegram_nudges
from tests.conftest import _make_pool_and_conn

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


# ---------------------------------------------------------------------------
# write_config -> validate_model_assignment: registry drift must not become
# an unhandled 500 (Task 3.4).
#
# validate_model_assignment (model_assignment.py:104) calls
# cloud_provider_key_present, which calls provider_for_id
# (llm_provider_registry.py:172-177). provider_for_id raises a bare
# ValueError for a provider id absent from PROVIDERS_BY_ID; model_assignment.py
# does not catch it, so config_write.py's write_config (the config-write
# boundary the settings router uses) must be the one that converts it into a
# clean HTTPException(400) instead of letting it surface as an unhandled 500.
# ---------------------------------------------------------------------------


def _patch_catalog_entry(monkeypatch: pytest.MonkeyPatch, *, provider: str) -> None:
    """Make catalog_entry_for_model return a fixed entry for any model id.

    Simulates "registry drift": a model-catalog entry whose provider id may
    or may not still exist in PROVIDERS_BY_ID.
    """
    fake_entry = SimpleNamespace(assignable=True, roles=("smart",), provider=provider)
    monkeypatch.setattr(
        "paper_ingestion.services.model_assignment.catalog_entry_for_model",
        lambda model_id: fake_entry,
    )


@pytest.mark.asyncio
async def test_write_config_unknown_provider_yields_400_not_500(monkeypatch):
    """A catalog entry pointing at a provider id not in PROVIDERS_BY_ID must
    surface as HTTPException(400), not an unhandled ValueError/500."""
    _patch_catalog_entry(monkeypatch, provider="ghost-vendor")

    with pytest.raises(HTTPException) as exc_info:
        await write_config(
            db_pool=MagicMock(),
            scheduler=MagicMock(),
            http_client=AsyncMock(),
            ollama_url="http://localhost:11434",
            key="llm.smart_model",
            value="some-cloud-model",
            caller_user_id=None,
        )

    assert exc_info.value.status_code == 400
    assert "ghost-vendor" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_write_config_known_provider_still_validates_normally(monkeypatch):
    """Regression guard: a valid provider id is unaffected by the new guard —
    the existing 422-on-missing-key path (a real HTTPException raised inside
    validate_model_assignment, not a ValueError) must propagate unchanged."""
    _patch_catalog_entry(monkeypatch, provider="anthropic")
    pool, _conn = _make_pool_and_conn(fetchrow_return=None)  # no API key configured

    with pytest.raises(HTTPException) as exc_info:
        await write_config(
            db_pool=pool,
            scheduler=MagicMock(),
            http_client=AsyncMock(),
            ollama_url="http://localhost:11434",
            key="llm.smart_model",
            value="claude-x",
            caller_user_id=None,
        )

    assert exc_info.value.status_code == 422
