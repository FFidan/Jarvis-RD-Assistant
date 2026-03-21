"""Helper to update LiteLLM config when user changes model assignments.

When a user selects a different Ollama model for a role (smart/fast/embed)
in the Settings UI, this module updates the litellm config.yaml to route
the alias to the new model. LiteLLM picks up the change on next request
(config is re-read from the mounted volume).
"""

import logging
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

# Mounted in docker-compose.yml as a shared volume
LITELLM_CONFIG_PATH = Path("/app/litellm_config/config.yaml")

ROLE_TO_ALIAS: dict[str, str] = {
    "llm.smart_model": "smart",
    "llm.fast_model": "fast",
    "llm.embed_model": "embed",
}


def update_litellm_model(config_key: str, ollama_model_name: str) -> bool:
    """Update litellm config.yaml to route an alias to a new Ollama model.

    Parameters
    ----------
    config_key : str
        The user_config key, e.g. ``'llm.smart_model'``.
    ollama_model_name : str
        The Ollama model name, e.g. ``'mistral-nemo'`` (without ``ollama/`` prefix).

    Returns
    -------
    bool
        True if the config was updated, False if the key is not a model role.
    """
    alias = ROLE_TO_ALIAS.get(config_key)
    if not alias:
        return False

    if not LITELLM_CONFIG_PATH.exists():
        logger.warning("LiteLLM config not found at %s", LITELLM_CONFIG_PATH)
        return False

    config = yaml.safe_load(LITELLM_CONFIG_PATH.read_text())
    updated = False
    for entry in config.get("model_list", []):
        if entry.get("model_name") == alias:
            old_model = entry.get("litellm_params", {}).get("model", "")
            new_model = f"ollama/{ollama_model_name}"
            if old_model != new_model:
                entry["litellm_params"]["model"] = new_model
                updated = True
                logger.info(
                    "Updated LiteLLM alias %r: %s -> %s",
                    alias, old_model, new_model,
                )
            break

    if updated:
        LITELLM_CONFIG_PATH.write_text(
            yaml.dump(config, default_flow_style=False, sort_keys=False)
        )

    return updated


async def reload_litellm() -> bool:
    """Signal LiteLLM to reload its config.

    Attempts to call the internal reload endpoint. If that fails,
    the config change will still take effect on LiteLLM's next restart.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # LiteLLM proxy supports config reload via internal API
            resp = await client.post(
                "http://litellm:4000/config/update",
                json={},
                headers={"Authorization": f"Bearer {_get_litellm_key()}"},
            )
            if resp.status_code < 400:
                logger.info("LiteLLM config reloaded successfully")
                return True
            logger.warning("LiteLLM reload returned %d", resp.status_code)
    except Exception as exc:
        logger.warning("Could not reload LiteLLM (will apply on restart): %r", exc)
    return False


def _get_litellm_key() -> str:
    """Read the LiteLLM master key from environment."""
    import os
    return os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "")
