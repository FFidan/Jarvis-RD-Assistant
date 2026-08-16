"""Model-assignment validation tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from jarvis_common.llm_provider_registry import PROVIDER_REGISTRY

from paper_ingestion.services.config_write import write_config
from paper_ingestion.services.model_assignment import (
    cloud_provider_key_present,
    provider_access_configured,
    validate_model_assignment,
)
from paper_ingestion.services.model_lifecycle import build_model_statuses
from paper_ingestion.services.provider_models import (
    live_model_entry,
    reset_provider_model_cache,
)
from tests.conftest import _make_pool_and_conn
from tests.test_provider_models import FakeConfigPool, Recorder, mock_http_client

# Registry drift is reported as a validation error at the configuration-write
# boundary instead of surfacing as an internal server error.


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


# ---------------------------------------------------------------------------
# One access predicate behind both the picker and the save gate
# ---------------------------------------------------------------------------

_CUSTOM_BASE_URL_KEY = "llm.providers.custom_openai_compatible.base_url"
_LOOPBACK_BASE_URL = "http://localhost:8000/v1"
_PRIVATE_BASE_URL = "http://10.0.0.5:8000/v1"
_UNKNOWN_NOTES = (
    "This provider did not say what this model can do, so JARVIS will not offer it for a role."
)


async def _assign(
    model_id: str,
    *,
    handler,
    config: dict[str, str] | None = None,
    key: str = "llm.smart_model",
) -> None:
    """Run the assignment save gate against a mocked provider endpoint."""
    reset_provider_model_cache()
    async with mock_http_client(handler) as client:
        await validate_model_assignment(
            http_client=client,
            ollama_url="http://ollama:11434",
            key=key,
            model_id=model_id,
            db_pool=FakeConfigPool(config),
        )


@pytest.mark.asyncio
async def test_live_listed_chat_model_passes_the_save_gate() -> None:
    await _assign(
        "openrouter/vendor/model-x",
        handler=Recorder({"data": [{"id": "vendor/model-x"}]}),
        config={"llm.providers.openrouter.api_key": "key"},
    )


@pytest.mark.asyncio
async def test_model_in_nobody_s_list_is_still_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _assign(
            "openrouter/vendor/ghost",
            handler=Recorder({"data": [{"id": "vendor/model-x"}]}),
            config={"llm.providers.openrouter.api_key": "key"},
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_live_listed_embedding_model_cannot_take_a_generative_role() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _assign(
            "openai/text-embedding-3-large",
            handler=Recorder({"data": [{"id": "text-embedding-3-large"}]}),
            config={"llm.openai.api_key": "key"},
        )

    assert exc_info.value.status_code == 422
    assert "embedding role is locked" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_display_only_model_is_refused_with_its_own_reason() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _assign(
            "openai/sora-2",
            handler=Recorder({"data": [{"id": "sora-2"}]}),
            config={"llm.openai.api_key": "key"},
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == _UNKNOWN_NOTES


@pytest.mark.asyncio
async def test_catalog_model_still_validates_when_the_provider_is_unreachable() -> None:
    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider down")

    await _assign(
        "anthropic/claude-sonnet-4-6",
        handler=unreachable,
        config={"llm.anthropic.api_key": "key"},
    )


@pytest.mark.asyncio
async def test_live_listed_model_from_a_keyless_keyed_provider_fails_the_access_gate() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _assign(
            "openrouter/vendor/model-x",
            handler=Recorder({"data": [{"id": "vendor/model-x"}]}),
            config={},
        )

    assert exc_info.value.status_code == 422
    assert "OpenRouter" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_base_url_only_custom_endpoint_model_passes_the_save_gate() -> None:
    """An endpoint reachable without a key must save, not just render enabled."""
    await _assign(
        "custom_openai/org/model-y",
        handler=Recorder({"data": [{"id": "org/model-y"}]}),
        config={_CUSTOM_BASE_URL_KEY: _LOOPBACK_BASE_URL},
    )


@pytest.mark.asyncio
async def test_a_cleared_base_url_does_not_count_as_configured() -> None:
    """An emptied row must read as absent, because every reader already treats it so.

    Counting it as present is the picker/save-gate divergence in a new place: the
    model renders enabled, saves, and then has no endpoint to deliver to.
    """
    access = await provider_access_configured(
        PROVIDER_REGISTRY,
        FakeConfigPool(
            {
                _CUSTOM_BASE_URL_KEY: "",
            }
        ),
    )

    assert access["custom_openai_compatible"] is False


@pytest.mark.asyncio
async def test_a_cleared_provider_key_does_not_count_as_configured() -> None:
    """The key path uses the same presence rule as the base-URL path, not a second one.

    A row cleared to the empty string reported the provider as configured, which
    left the account fetch and the Test button enabled for a credential that
    cannot authenticate anything.
    """
    key = "llm.providers.openrouter.api_key"

    assert await cloud_provider_key_present("openrouter", FakeConfigPool({key: ""})) is False
    assert await cloud_provider_key_present("openrouter", FakeConfigPool({key: "sk-test"})) is True


@pytest.mark.asyncio
async def test_base_url_only_custom_endpoint_model_renders_enabled_in_the_picker() -> None:
    """The picker's presence map is the same predicate the save gate uses."""
    access = await provider_access_configured(
        PROVIDER_REGISTRY, FakeConfigPool({_CUSTOM_BASE_URL_KEY: _LOOPBACK_BASE_URL})
    )
    entry = live_model_entry(
        "custom_openai_compatible",
        "org/model-y",
        fetched_at=None,
    )

    statuses = build_model_statuses(
        installed=[],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=None,
        cloud_api_keys=access,
        extra_entries=(entry,),
    )
    item = next(i for i in statuses if i["id"] == "custom_openai/org/model-y")

    assert access["custom_openai_compatible"] is True
    assert item["can_assign"] is True


@pytest.mark.asyncio
async def test_a_private_endpoint_url_is_refused_when_it_is_stored() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await write_config(
            db_pool=FakeConfigPool(),
            scheduler=MagicMock(),
            http_client=AsyncMock(),
            ollama_url="http://localhost:11434",
            key=_CUSTOM_BASE_URL_KEY,
            value=_PRIVATE_BASE_URL,
            caller_user_id=None,
        )

    assert exc_info.value.status_code == 400
    assert "blocked network address" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_access_predicate_covers_every_registered_provider() -> None:
    access = await provider_access_configured(
        PROVIDER_REGISTRY, FakeConfigPool({"llm.providers.deepseek.api_key": "key"})
    )

    assert {provider.id for provider in PROVIDER_REGISTRY} == set(access)
    assert access["deepseek"] is True
    assert access["mistral"] is False
