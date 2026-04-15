"""Helper to update LiteLLM config when user changes model assignments.

When a user selects a different Ollama model for a role (smart/fast/embed)
in the Settings UI, this module updates the litellm config.yaml to route
the alias to the new model. LiteLLM picks up the change on next request
(config is re-read from the mounted volume).

Note: the litellm config mount is read-only in production (SEC-002).
``update_litellm_model`` validates the model name via an allowlist and raises
``RuntimeError`` on any OS-level write failure so the router can return a
clear 400 instead of a confusing IO error.
"""

import asyncio
import logging
import re
from pathlib import Path

import httpx
import yaml
from jarvis_common.llm_client import LITELLM_FALLBACK_ENV_NAMES, get_litellm_config

logger = logging.getLogger(__name__)


def _validate_model_name(ollama_model_name: str) -> None:
    """Reject model names that contain path traversal or shell metacharacters.

    The model name is interpolated into a YAML string; a value like
    ``../../etc/passwd`` or ``; rm -rf /`` must never reach the config file.
    Permit only ``[a-zA-Z0-9._:-]`` characters (covers all real Ollama IDs).
    """
    if not re.fullmatch(r"[a-zA-Z0-9._:\-]+", ollama_model_name):
        raise ValueError(
            f"Model name {ollama_model_name!r} contains disallowed characters. "
            "Only alphanumerics and . _ : - are permitted."
        )


# Mounted in docker-compose.yml as a shared volume
LITELLM_CONFIG_PATH = Path("/app/litellm_config/config.yaml")

ROLE_TO_ALIAS: dict[str, str] = {
    "llm.smart_model": "smart",
    "llm.fast_model": "fast",
    "llm.embed_model": "embed",
}

# Async-safe lock for config file I/O (A3: PI-009 + PI-010)
_config_lock = asyncio.Lock()


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

    Raises
    ------
    ValueError
        If ``ollama_model_name`` contains disallowed characters (SEC-002).
    RuntimeError
        If the config file is read-only (SEC-002 mount is ``:ro`` in production).
    """
    # SEC-002: validate model name before any file I/O
    _validate_model_name(ollama_model_name)

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
            # A6: detect provider from existing entry instead of hardcoding ollama
            existing_model = (entry.get("litellm_params") or {}).get("model", "")
            if "/" in existing_model:
                provider = existing_model.split("/")[0]
                new_model = f"{provider}/{ollama_model_name}"
            else:
                new_model = f"ollama/{ollama_model_name}"
            if existing_model != new_model:
                # A2: guard litellm_params null in YAML update
                params = entry.get("litellm_params")
                if params is None:
                    params = {}
                    entry["litellm_params"] = params
                params["model"] = new_model
                updated = True
                logger.info(
                    "Updated LiteLLM alias %r: %s -> %s",
                    alias,
                    existing_model,
                    new_model,
                )
            break

    if updated:
        try:
            LITELLM_CONFIG_PATH.write_text(
                yaml.dump(config, default_flow_style=False, sort_keys=False)
            )
        except OSError as exc:
            # SEC-002: mount is :ro in production — surface a clear error so
            # the router can return HTTP 400 instead of a confusing 500.
            raise RuntimeError(
                "LiteLLM config is read-only; model alias updates are disabled "
                "in this deployment. Restart LiteLLM with an updated config file "
                "to change model assignments."
            ) from exc

    return updated


async def reload_litellm() -> bool:
    """Signal LiteLLM to reload its config.

    Attempts to call the internal reload endpoint. If that fails,
    the config change will still take effect on LiteLLM's next restart.
    """
    try:
        # A5: use shared helper instead of local _get_litellm_key
        litellm_cfg = get_litellm_config(
            fallback_env_names=LITELLM_FALLBACK_ENV_NAMES,
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            # LiteLLM proxy supports config reload via internal API
            resp = await client.post(
                f"{litellm_cfg.base_url}/config/update",
                json={},
                headers={"Authorization": f"Bearer {litellm_cfg.api_key}"},
            )
            if resp.status_code < 400:
                logger.info("LiteLLM config reloaded successfully")
                return True
            # A4: improved logging on non-2xx response
            logger.warning(
                "LiteLLM reload returned %s — config will apply on next LiteLLM restart",
                resp.status_code,
            )
    except Exception as exc:
        logger.warning(
            "Could not signal LiteLLM to reload — config will apply on next LiteLLM restart: %r",
            exc,
        )
    return False
