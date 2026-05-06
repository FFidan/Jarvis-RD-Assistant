"""Tests for cloud-provider API key injection in litellm_config.

Covers:
- get_provider_api_key: encrypted, legacy plaintext, missing, unsupported provider
- update_litellm_model: cloud key injected into POST /config/update payload,
  fallback + warning when key is missing, local Ollama models untouched by DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from paper_ingestion.services.litellm_config import (
    get_provider_api_key,
    update_litellm_model,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Create a mock asyncpg Pool + Connection pair."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


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
        "SELECT value, encrypted_value FROM user_config WHERE key = $1",
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
    """Cloud model name triggers POST to /config/update with api_key in payload."""
    from cryptography.fernet import Fernet

    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", fernet_key)
    from jarvis_common.crypto import refresh_fernet_cache

    refresh_fernet_cache()
    from jarvis_common.crypto import encrypt_secret

    plaintext_key = "sk-ant-api-test-key"
    ciphertext = encrypt_secret(plaintext_key)

    # Wire env so get_litellm_config() finds the base_url.
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm-test:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test")

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext.encode("ascii"),
    }

    # Mock the POST to LiteLLM.
    config_update_route = respx.post("http://litellm-test:4000/config/update").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    result = await update_litellm_model("smart", "anthropic/claude-sonnet-4-5", db_pool=pool)

    assert result is True
    assert config_update_route.called, "Expected POST to LiteLLM /config/update"

    # Verify payload contains api_key under litellm_params.
    posted_json = config_update_route.calls.last.request
    import json

    body = json.loads(posted_json.content)
    assert body["model_list"][0]["model_name"] == "smart"
    assert body["model_list"][0]["litellm_params"]["model"] == "anthropic/claude-sonnet-4-5"
    assert body["model_list"][0]["litellm_params"]["api_key"] == plaintext_key
    assert posted_json.headers["Authorization"] == "Bearer sk-master-test"


@respx.mock
@pytest.mark.asyncio
async def test_update_litellm_model_raises_when_cloud_update_fails(monkeypatch):
    """Cloud assignment failures must surface to the settings router."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm-test:4000")
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "value": "sk-openai-test",
        "encrypted_value": None,
    }
    respx.post("http://litellm-test:4000/config/update").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )

    with pytest.raises(RuntimeError, match="LiteLLM /config/update failed"):
        await update_litellm_model("smart", "openai/gpt-4o", db_pool=pool)


@pytest.mark.asyncio
async def test_update_litellm_model_no_key_falls_back_and_warns(tmp_path, monkeypatch, caplog):
    """Missing provider key logs a warning and falls back to the Ollama YAML path."""

    import paper_ingestion.services.litellm_config as mod
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "model_list": [
                    {
                        "model_name": "smart",
                        "litellm_params": {"model": "ollama/mistral-nemo"},
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(mod, "LITELLM_CONFIG_PATH", config_path)

    pool, conn = _make_pool_and_conn()
    # No key configured → fetchrow returns None.
    conn.fetchrow.return_value = None

    import logging

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.litellm_config"):
        result = await update_litellm_model("smart", "anthropic/claude-opus-4-5", db_pool=pool)

    # Should fall back to YAML path and write the model string as-is.
    assert "falling back" in caplog.text.lower()

    # The YAML file should have been updated with the full model string.
    updated = yaml.safe_load(config_path.read_text())
    assert updated["model_list"][0]["litellm_params"]["model"] == "anthropic/claude-opus-4-5"
    # Result reflects whether the YAML was actually changed.
    assert result is True


@pytest.mark.asyncio
async def test_update_litellm_model_local_model_unchanged(monkeypatch):
    """Ollama/local model does not trigger any DB read for provider keys."""
    from pathlib import Path
    from unittest.mock import patch

    import paper_ingestion.services.litellm_config as mod
    import yaml

    # We don't need a real YAML for this test — just prove DB is never hit.
    pool = MagicMock()
    pool.acquire = MagicMock()

    # Patch get_provider_api_key so we can assert it was never called.
    with patch.object(mod, "get_provider_api_key", new_callable=AsyncMock) as mock_gpa:
        # Provide a real config path so the YAML path can execute.
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tf:
            yaml.dump(
                {
                    "model_list": [
                        {
                            "model_name": "smart",
                            "litellm_params": {"model": "ollama/mistral-nemo"},
                        }
                    ]
                },
                tf,
            )
            tmp_yaml = Path(tf.name)

        monkeypatch.setattr(mod, "LITELLM_CONFIG_PATH", tmp_yaml)

        await update_litellm_model("smart", "ollama/mistral-nemo", db_pool=pool)

        # get_provider_api_key must NOT have been called — this is a local model.
        mock_gpa.assert_not_called()

        tmp_yaml.unlink(missing_ok=True)
