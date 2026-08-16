"""Tests for cloud-provider API key injection in litellm_config.

Covers:
- get_provider_api_key: encrypted, legacy plaintext, missing, unsupported provider
- update_litellm_model: cloud key carried in the POST /model/new payload (stored
  by LiteLLM encrypted under the pinned LITELLM_SALT_KEY in its admin DB),
  keyless delivery + warning when no key is configured, local Ollama models
  never reading provider keys.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from paper_ingestion.services.litellm_config import (
    _CLOUD_DELIVERED_FINGERPRINTS,
    get_provider_api_key,
    update_litellm_model,
)

from tests.conftest import _make_pool_and_conn

LITELLM = "http://litellm-test:4000"


@pytest.fixture(autouse=True)
def _clear_cloud_delivery_fingerprints():
    """Keep process-local LiteLLM delivery fingerprints isolated per test."""
    _CLOUD_DELIVERED_FINGERPRINTS.clear()
    yield
    _CLOUD_DELIVERED_FINGERPRINTS.clear()


def _entry(
    alias: str,
    params: dict[str, Any],
    *,
    dep_id: str = "dep-1",
    db_model: bool = True,
) -> dict[str, Any]:
    return {
        "model_name": alias,
        "litellm_params": params,
        "model_info": {"id": dep_id, "db_model": db_model},
    }


# ---------------------------------------------------------------------------
# get_provider_api_key tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_provider_api_key_encrypted(monkeypatch):
    """Encrypted value in DB is decrypted and returned as plaintext."""
    # Seed a real Fernet key so encrypt_secret/decrypt_secret work.
    from cryptography.fernet import Fernet

    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", fernet_key)

    # Refresh the lru_cache so the new key is picked up.
    from jarvis_common.crypto import refresh_fernet_cache

    refresh_fernet_cache()

    from jarvis_common.crypto import encrypt_secret

    plaintext = "sk-ant-test-key-1234"
    ciphertext = encrypt_secret(plaintext)

    pool, conn = _make_pool_and_conn()
    # DB returns encrypted_value as bytes (BYTEA), value as None.
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext.encode("ascii"),
    }

    result = await get_provider_api_key("anthropic", pool)
    assert result == plaintext

    # Verify correct key was queried.
    conn.fetchrow.assert_awaited_once_with(
        "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
        "llm.anthropic.api_key",
    )


@pytest.mark.asyncio
async def test_get_provider_api_key_legacy_plaintext():
    """Legacy plaintext value (only `value` column set) is returned directly."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "value": "sk-open-ai-legacy-key",
        "encrypted_value": None,
    }

    result = await get_provider_api_key("openai", pool)
    assert result == "sk-open-ai-legacy-key"


@pytest.mark.asyncio
async def test_get_provider_api_key_missing_returns_none():
    """When no row exists for the key, None is returned."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None

    result = await get_provider_api_key("google", pool)
    assert result is None


@pytest.mark.asyncio
async def test_get_provider_api_key_both_null_returns_none():
    """When both value and encrypted_value are NULL, None is returned."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": None,
    }

    result = await get_provider_api_key("anthropic", pool)
    assert result is None


@pytest.mark.asyncio
async def test_get_provider_api_key_unsupported_raises():
    """Unknown provider name raises ValueError."""
    pool, _ = _make_pool_and_conn()
    with pytest.raises(ValueError, match="Unsupported provider"):
        await get_provider_api_key("cohere", pool)


# ---------------------------------------------------------------------------
# update_litellm_model — cloud path tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_injects_cloud_key_and_master_key(monkeypatch):
    """Cloud model → /model/new payload carries the decrypted api_key + master-key auth.

    The plaintext key exists only in memory and in this request; LiteLLM
    persists it encrypted under the pinned LITELLM_SALT_KEY in its admin DB.
    """
    from cryptography.fernet import Fernet

    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", fernet_key)
    from jarvis_common.crypto import refresh_fernet_cache

    refresh_fernet_cache()
    from jarvis_common.crypto import encrypt_secret

    plaintext_key = "sk-ant-api-test-key"
    ciphertext = encrypt_secret(plaintext_key)

    # Wire env so get_litellm_config() finds the base_url.
    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test")

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext.encode("ascii"),
    }

    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_entry("smart", {"model": "ollama_chat/qwen3:8b"}, dep_id="old-1")]},
        )
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await update_litellm_model("smart", "anthropic/claude-sonnet-4-5", db_pool=pool)

    assert result is True
    assert new_route.called, "Expected POST to LiteLLM /model/new"

    request = new_route.calls.last.request
    body = json.loads(request.content)
    assert body["model_name"] == "smart"
    assert body["litellm_params"]["model"] == "anthropic/claude-sonnet-4-5"
    assert body["litellm_params"]["api_key"] == plaintext_key
    # Ollama-only transport params must not leak onto the cloud deployment.
    assert "api_base" not in body["litellm_params"]
    assert "num_ctx" not in body["litellm_params"]
    assert request.headers["Authorization"] == "Bearer sk-master-test"
    # The superseded local deployment is removed.
    assert json.loads(delete_route.calls.last.request.content) == {"id": "old-1"}


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_raises_when_cloud_delivery_fails(monkeypatch):
    """Cloud assignment failures must surface to the settings router."""
    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "value": "sk-openai-test",
        "encrypted_value": None,
    }
    respx.get(f"{LITELLM}/v1/model/info").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )

    with pytest.raises(RuntimeError, match="LiteLLM /model/new failed"):
        await update_litellm_model("smart", "openai/gpt-4o", db_pool=pool)


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_no_key_delivers_keyless_and_warns(monkeypatch, caplog):
    """Missing provider key logs a warning; the deployment is delivered without a key."""
    import logging

    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    pool, conn = _make_pool_and_conn()
    # No key configured → fetchrow returns None.
    conn.fetchrow.return_value = None

    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_entry("smart", {"model": "ollama_chat/mistral-nemo"}, dep_id="old-1")]},
        )
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.litellm_config"):
        result = await update_litellm_model("smart", "anthropic/claude-opus-4-5", db_pool=pool)

    assert result is True
    assert "without a key" in caplog.text.lower()
    body = json.loads(new_route.calls.last.request.content)
    assert body["litellm_params"]["model"] == "anthropic/claude-opus-4-5"
    assert "api_key" not in body["litellm_params"]


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_local_model_never_reads_provider_keys(monkeypatch):
    """Ollama/local model does not trigger any DB read for provider keys."""
    from unittest.mock import patch

    import paper_ingestion.services.litellm_config as mod

    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    pool, _conn = _make_pool_and_conn()

    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_entry("smart", {"model": "ollama_chat/mistral-nemo"})]},
        )
    )

    with patch.object(mod, "get_provider_api_key", new_callable=AsyncMock) as mock_gpa:
        # Same model → no-op; the relevant assertion is the key read below.
        await update_litellm_model("smart", "ollama_chat/mistral-nemo", db_pool=pool)

        # get_provider_api_key must NOT have been called — this is a local model.
        mock_gpa.assert_not_called()


@pytest.mark.asyncio
async def test_get_provider_api_key_reads_registry_key():
    """New providers use registry-owned llm.providers.<id>.api_key rows."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "value": "sk-or-test",
        "encrypted_value": None,
    }

    result = await get_provider_api_key("openrouter", pool)

    assert result == "sk-or-test"
    conn.fetchrow.assert_awaited_once_with(
        "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
        "llm.providers.openrouter.api_key",
    )


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_delivers_custom_openai_endpoint(monkeypatch):
    """Custom OpenAI-compatible routes use openai/<model> plus stored api_base."""
    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        {"value": "sk-custom", "encrypted_value": None},
        {"value": "http://127.0.0.1:8000/v1", "encrypted_value": None},
    ]

    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_entry("smart", {"model": "ollama_chat/qwen3:8b"}, dep_id="old-1")]},
        )
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    result = await update_litellm_model(
        "smart",
        "custom_openai/local-model",
        db_pool=pool,
    )

    assert result is True
    params = json.loads(new_route.calls.last.request.content)["litellm_params"]
    assert params["model"] == "openai/local-model"
    assert params["api_base"] == "http://127.0.0.1:8000/v1"
    assert params["api_key"] == "sk-custom"


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_redelivers_custom_endpoint_base_url_change(monkeypatch):
    """Changing only api_base must re-deliver the custom OpenAI-compatible route."""
    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        {"value": "sk-custom", "encrypted_value": None},
        {"value": "http://127.0.0.1:8000/v1", "encrypted_value": None},
        {"value": "sk-custom", "encrypted_value": None},
        {"value": "http://127.0.0.1:8001/v1", "encrypted_value": None},
    ]

    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_entry("smart", {"model": "openai/local-model"}, dep_id="old-1")]},
        )
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    assert await update_litellm_model("smart", "custom_openai/local-model", db_pool=pool) is True
    assert await update_litellm_model("smart", "custom_openai/local-model", db_pool=pool) is True

    assert new_route.call_count == 2
    params = json.loads(new_route.calls.last.request.content)["litellm_params"]
    assert params["api_base"] == "http://127.0.0.1:8001/v1"


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_rejects_blocked_custom_endpoint(monkeypatch):
    """Blocked custom endpoints fail before LiteLLM receives a deployment."""
    import jarvis_common.llm_provider_registry as registry

    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        {"value": "sk-custom", "encrypted_value": None},
        {"value": "https://llm.example.test/v1", "encrypted_value": None},
    ]

    def fake_getaddrinfo(*_args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(registry.socket, "getaddrinfo", fake_getaddrinfo)

    respx.get(f"{LITELLM}/v1/model/info").mock(return_value=httpx.Response(200, json={"data": []}))
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )

    with pytest.raises(RuntimeError, match="custom provider endpoint is blocked"):
        await update_litellm_model("smart", "custom_openai/local-model", db_pool=pool)

    assert not new_route.called
