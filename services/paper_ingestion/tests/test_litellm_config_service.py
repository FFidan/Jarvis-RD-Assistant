"""Tests for LiteLLM config update and reload."""

from pathlib import Path

import pytest
import yaml
from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS, update_litellm_model


def _write_config(path: Path, model_list: list[dict]) -> None:
    """Write a minimal litellm config.yaml."""
    path.write_text(yaml.dump({"model_list": model_list}, default_flow_style=False))


def test_role_to_alias_covers_all_llm_keys():
    """ROLE_TO_ALIAS should map all llm.* config keys."""
    assert "llm.smart_model" in ROLE_TO_ALIAS
    assert "llm.fast_model" in ROLE_TO_ALIAS
    assert "llm.embed_model" in ROLE_TO_ALIAS


@pytest.mark.asyncio
async def test_update_known_role_rewrites_yaml(tmp_path, monkeypatch):
    """Updating a known role should rewrite the YAML with the new model."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            {"model_name": "smart", "litellm_params": {"model": "ollama/mistral-nemo"}},
        ],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    result = await update_litellm_model("llm.smart_model", "qwen3:4b")
    assert result is True

    updated = yaml.safe_load(config_path.read_text())
    assert updated["model_list"][0]["litellm_params"]["model"] == "ollama/qwen3:4b"


@pytest.mark.asyncio
async def test_update_unknown_role_returns_false(tmp_path, monkeypatch):
    """A config key not in ROLE_TO_ALIAS should return False without touching the file."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, [])
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    assert await update_litellm_model("ui.page_size", "10") is False


@pytest.mark.asyncio
async def test_update_missing_config_returns_false(tmp_path, monkeypatch):
    """If the config file does not exist, return False gracefully."""
    monkeypatch.setattr(
        "paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", tmp_path / "nope.yaml"
    )
    assert await update_litellm_model("llm.smart_model", "test") is False


@pytest.mark.asyncio
async def test_update_preserves_provider_prefix(tmp_path, monkeypatch):
    """If the existing model uses a non-ollama provider prefix, preserve it."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            {"model_name": "smart", "litellm_params": {"model": "openai/gpt-4"}},
        ],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    await update_litellm_model("llm.smart_model", "gpt-4-turbo")
    updated = yaml.safe_load(config_path.read_text())
    assert updated["model_list"][0]["litellm_params"]["model"] == "openai/gpt-4-turbo"


@pytest.mark.asyncio
async def test_update_null_litellm_params(tmp_path, monkeypatch):
    """If litellm_params is None/null in the YAML, create it and set the model."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            {"model_name": "smart", "litellm_params": None},
        ],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    result = await update_litellm_model("llm.smart_model", "mistral-nemo")
    assert result is True
    updated = yaml.safe_load(config_path.read_text())
    assert updated["model_list"][0]["litellm_params"]["model"] == "ollama/mistral-nemo"


@pytest.mark.asyncio
async def test_same_model_no_update(tmp_path, monkeypatch):
    """If the model is already set to the same value, no write should happen."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            {"model_name": "smart", "litellm_params": {"model": "ollama/mistral-nemo"}},
        ],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)
    mtime_before = config_path.stat().st_mtime

    result = await update_litellm_model("llm.smart_model", "mistral-nemo")
    assert result is False  # No change needed
    # File should not have been rewritten
    assert config_path.stat().st_mtime == mtime_before


@pytest.mark.asyncio
async def test_update_no_provider_prefix_defaults_to_ollama(tmp_path, monkeypatch):
    """If the existing model has no provider prefix, default to ollama/."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            {"model_name": "fast", "litellm_params": {"model": "qwen3:4b"}},
        ],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    result = await update_litellm_model("llm.fast_model", "phi3:mini")
    assert result is True
    updated = yaml.safe_load(config_path.read_text())
    assert updated["model_list"][0]["litellm_params"]["model"] == "ollama/phi3:mini"


@pytest.mark.asyncio
async def test_update_embed_model(tmp_path, monkeypatch):
    """Embed model alias should also be updatable."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            {"model_name": "embed", "litellm_params": {"model": "ollama/qwen3-embedding:0.6b"}},
        ],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    result = await update_litellm_model("llm.embed_model", "mxbai-embed-large")
    assert result is True
    updated = yaml.safe_load(config_path.read_text())
    assert updated["model_list"][0]["litellm_params"]["model"] == "ollama/mxbai-embed-large"


@pytest.mark.asyncio
async def test_update_leaves_other_entries_untouched(tmp_path, monkeypatch):
    """Updating one model alias should not affect other entries in model_list."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            {"model_name": "smart", "litellm_params": {"model": "ollama/mistral-nemo"}},
            {"model_name": "fast", "litellm_params": {"model": "ollama/qwen3:4b"}},
            {"model_name": "embed", "litellm_params": {"model": "ollama/qwen3-embedding:0.6b"}},
        ],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    await update_litellm_model("llm.fast_model", "phi3:mini")
    updated = yaml.safe_load(config_path.read_text())

    # fast should be updated
    assert updated["model_list"][1]["litellm_params"]["model"] == "ollama/phi3:mini"
    # smart and embed should be unchanged
    assert updated["model_list"][0]["litellm_params"]["model"] == "ollama/mistral-nemo"
    assert updated["model_list"][2]["litellm_params"]["model"] == "ollama/qwen3-embedding:0.6b"


@pytest.mark.asyncio
async def test_update_litellm_model_falls_back_to_in_memory_on_ro_config(tmp_path, monkeypatch):
    """When YAML write raises OSError (:ro mount), falls back to /config/update."""
    import httpx as _httpx
    import respx

    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [{"model_name": "smart", "litellm_params": {"model": "ollama/mistral-nemo"}}],
    )
    monkeypatch.setattr("paper_ingestion.services.litellm_config.LITELLM_CONFIG_PATH", config_path)

    write_calls = {"n": 0}

    def _fail_write(self, content, **kwargs):
        write_calls["n"] += 1
        raise OSError("Read-only file system")

    monkeypatch.setattr(config_path.__class__, "write_text", _fail_write)

    with respx.mock:
        respx.post("http://litellm:4000/config/update").mock(
            return_value=_httpx.Response(200, json={"message": "ok"})
        )
        result = await update_litellm_model("llm.smart_model", "qwen3:4b")

        assert result is True
        assert write_calls["n"] == 1  # write was attempted once
        assert respx.calls.call_count == 1  # fallback was invoked
