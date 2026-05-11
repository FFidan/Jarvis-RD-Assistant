"""Helper to update LiteLLM config when user changes model assignments.

When a user selects a different Ollama model for a role (smart/fast/embed)
in the Settings UI, this module updates the litellm config.yaml to route
the alias to the new model. LiteLLM picks up the change on next request
(config is re-read from the mounted volume).

For cloud-provider models (anthropic/, openai/, gemini/) the YAML is
read-only (SEC-002), so the model + API key are injected via LiteLLM's
``POST /config/update`` endpoint in-memory instead of writing to disk.

Note: the litellm config mount is read-only in production (SEC-002).
``update_litellm_model`` validates the model name via an allowlist and raises
``RuntimeError`` on any OS-level write failure so the router can return a
clear 400 instead of a confusing IO error.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import httpx
import yaml
from jarvis_common.crypto import resolve_secret_row
from jarvis_common.llm_client import build_litellm_headers, get_litellm_config

logger = logging.getLogger(__name__)

# Serializes concurrent LiteLLM config updates — the settings router imports this
# to guard PUT /api/config/{llm.*_model} against racy YAML rewrites / overlapping
# POST /config/update calls.
_config_lock = asyncio.Lock()  # pyright: ignore[reportUnusedVariable]  # imported from routers/settings.py


class ProviderKeyMissing(Exception):  # noqa: N818
    """Raised when a cloud-provider API key is required but not configured."""


# Map from LiteLLM model prefix → canonical provider name used in user_config keys.
# gemini/ is the LiteLLM prefix for Google models; the config key uses "google".
_CLOUD_PREFIX_TO_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "google",
}

_ALLOWED_PROVIDERS = frozenset({"anthropic", "openai", "google"})


async def get_provider_api_key(provider: str, db_pool: Any) -> str | None:
    """Fetch and decrypt the LLM provider API key from user_config.

    Returns plaintext key, or None if not configured.

    Parameters
    ----------
    provider:
        One of ``"anthropic"``, ``"openai"``, ``"google"``.
    db_pool:
        asyncpg Pool instance.

    Raises
    ------
    ValueError
        If *provider* is not in the allowed set.
    """
    if provider not in _ALLOWED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider {provider!r}. Allowed values: {sorted(_ALLOWED_PROVIDERS)}"
        )

    config_key = f"llm.{provider}.api_key"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
            config_key,
        )

    if row is None:
        return None
    return resolve_secret_row(row)


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


async def _get_thinking_disabled(
    model_name: str,
    machine_id: str,
    db_pool: Any,
) -> bool:
    """Return True if the user has disabled thinking mode for *model_name* on *machine_id*.

    Reads ``llm.{machine_id}.thinking_disabled.{model_name}`` from user_config.
    Returns False if the key is absent, db_pool is None, or any error occurs.
    """
    if not machine_id or db_pool is None:
        return False
    config_key = f"llm.{machine_id}.thinking_disabled.{model_name}"
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
                config_key,
            )
        if row is None:
            return False
        val = row["value"]
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)
    except Exception:
        logger.warning(
            "Could not read thinking_disabled key for %r on machine %r",
            model_name,
            machine_id,
            exc_info=True,
        )
        return False


async def update_litellm_model(
    config_key: str,
    model_name: str,
    db_pool: Any = None,
    machine_id: str = "",
) -> bool:
    """Route an alias to a new model, injecting cloud API keys when needed.

    For local Ollama models the existing litellm config.yaml is updated on
    disk (SEC-002: only valid when the mount is read-write).  For cloud-provider
    models (``anthropic/``, ``openai/``, ``gemini/``) the change is delivered
    via LiteLLM's ``POST /config/update`` endpoint instead — the YAML is never
    written (SEC-002 compliance).

    Parameters
    ----------
    config_key : str
        Either the user_config key (``'llm.smart_model'``) or the alias
        directly (``'smart'``).  Both forms are accepted for convenience.
    model_name : str
        The full model string, e.g. ``'mistral-nemo'``, ``'ollama/mistral-nemo'``
        or ``'anthropic/claude-sonnet-4-5'``.
    db_pool : asyncpg Pool, optional
        Required when *model_name* has a cloud-provider prefix so that the
        encrypted API key can be fetched, or when thinking-mode propagation
        is desired.
    machine_id : str, optional
        The current machine's hostname.  When provided together with *db_pool*,
        ``update_litellm_model`` reads the
        ``llm.{machine_id}.thinking_disabled.{model_name}`` user_config flag for
        thinking-capable models and includes ``extra_body: {think: false}`` in
        the LiteLLM params when that flag is set.

    Returns
    -------
    bool
        ``True`` if the alias was updated (YAML written or POST succeeded),
        ``False`` if the key is not a model role or no change was needed.

    Raises
    ------
    ValueError
        If *model_name* contains disallowed characters (SEC-002).
    RuntimeError
        If the config file is read-only (SEC-002 mount is ``:ro``).
    """
    # Resolve config_key → alias.  Accept either format for convenience.
    alias = ROLE_TO_ALIAS.get(config_key) or (
        config_key if config_key in ROLE_TO_ALIAS.values() else None
    )
    if not alias:
        return False

    # -------------------------------------------------------------------------
    # Detect cloud-provider prefix.  gemini/ maps to the "google" provider name.
    # -------------------------------------------------------------------------
    cloud_provider: str | None = None
    # Normalize: strip :latest — Ollama's default implicit tag is never stored in
    # config.yaml, so "mistral-nemo:latest" and "mistral-nemo" must be treated as equal.
    if model_name.endswith(":latest"):
        model_name = model_name[:-7]
    model_suffix = model_name  # the part after provider/ (or full name for Ollama)

    if "/" in model_name:
        prefix, model_suffix = model_name.split("/", 1)
        cloud_provider = _CLOUD_PREFIX_TO_PROVIDER.get(prefix)
        # model_suffix already holds the part after "/" — validate only that part.

    # SEC-002: validate the model-name portion (no path traversal / shell chars).
    # For "provider/model-name" strings we validate only the model-name suffix.
    _validate_model_name(model_suffix)

    # -------------------------------------------------------------------------
    # Thinking-mode propagation.
    # For thinking-capable models (supports_thinking=True in catalog), read the
    # per-machine thinking_disabled flag and build an extra_body override when set.
    # -------------------------------------------------------------------------
    extra_body: dict[str, Any] | None = None
    # Import here to avoid circular import at module level
    from jarvis_common.model_catalog import ModelCatalogEntry, load_model_catalog  # noqa: PLC0415

    catalog = load_model_catalog()
    catalog_entry: ModelCatalogEntry | None = None
    bare_model = model_name.split("/", 1)[-1] if "/" in model_name else model_name
    for _entry in catalog:
        entry_bare = (_entry.ollama_tag or _entry.id).split("/")[-1]
        if entry_bare == bare_model or _entry.id == model_name:
            catalog_entry = _entry
            break
    if catalog_entry is not None and catalog_entry.supports_thinking:
        thinking_disabled = await _get_thinking_disabled(model_name, machine_id, db_pool)
        if thinking_disabled:
            extra_body = {"think": False}
            logger.info(
                "Thinking mode disabled for model %r on machine %r (alias %r)",
                model_name,
                machine_id,
                alias,
            )

    # -------------------------------------------------------------------------
    # Cloud-provider path: POST to /config/update (never touch the YAML file).
    # -------------------------------------------------------------------------
    if cloud_provider is not None:
        if db_pool is None:
            logger.warning(
                "Cannot inject cloud API key for alias %r — db_pool not provided; "
                "falling back to Ollama alias update",
                alias,
            )
            cloud_provider = None  # fall through to YAML path below
        else:
            api_key = await get_provider_api_key(cloud_provider, db_pool)
            if api_key is None:
                logger.warning(
                    "No API key configured for provider %r (alias %r) — "
                    "falling back to Ollama alias update",
                    cloud_provider,
                    alias,
                )
                cloud_provider = None  # fall through to YAML path below
            else:
                return await _post_config_update(alias, model_name, api_key, extra_body=extra_body)

    # -------------------------------------------------------------------------
    # Local / Ollama path: update the YAML file on disk.
    # For models without a provider prefix, prepend ollama/.
    # For models whose existing YAML entry already has a provider prefix, reuse it.
    # -------------------------------------------------------------------------
    if not LITELLM_CONFIG_PATH.exists():
        logger.warning("LiteLLM config not found at %s", LITELLM_CONFIG_PATH)
        return False

    config = yaml.safe_load(LITELLM_CONFIG_PATH.read_text())
    updated = False
    new_model = model_name  # fallback; set in loop below when alias found
    for entry in config.get("model_list", []):
        if entry.get("model_name") == alias:
            existing_model = (entry.get("litellm_params") or {}).get("model", "")
            # Compute the new model string.
            if "/" in model_name:
                # Caller supplied a full provider/model string.
                new_model = model_name
            elif "/" in existing_model:
                # Inherit the existing provider prefix (A6).
                existing_prefix = existing_model.split("/")[0]
                new_model = f"{existing_prefix}/{model_name}"
            else:
                new_model = f"ollama/{model_name}"

            # A2: guard litellm_params null in YAML.
            params = entry.get("litellm_params")
            if params is None:
                params = {}
                entry["litellm_params"] = params

            model_changed = existing_model != new_model
            if model_changed:
                params["model"] = new_model
                logger.info(
                    "Updated LiteLLM alias %r: %s -> %s",
                    alias,
                    existing_model,
                    new_model,
                )

            # Propagate thinking-mode override regardless of model change.
            if extra_body is not None:
                params["extra_body"] = extra_body
                updated = True
                logger.info("Injected extra_body=%r into LiteLLM alias %r", extra_body, alias)

            if model_changed:
                updated = True
            break

    if updated:
        try:
            LITELLM_CONFIG_PATH.write_text(
                yaml.dump(config, default_flow_style=False, sort_keys=False)
            )
        except OSError:
            logger.warning("LiteLLM config is :ro; falling back to in-memory /config/update")
            return await _post_ollama_alias_update(alias, new_model, extra_body=extra_body)

    return updated


async def _post_config_update(
    alias: str,
    model_name: str,
    api_key: str,
    extra_body: dict[str, Any] | None = None,
) -> bool:
    """POST a model alias update to LiteLLM's /config/update endpoint.

    The API key is injected into the in-memory litellm_params dict and is
    **never** written to the YAML file on disk (SEC-002).

    Returns True on success (HTTP < 400), False otherwise.
    """
    litellm_params: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
    }
    if extra_body is not None:
        litellm_params["extra_body"] = extra_body
    payload: dict[str, Any] = {
        "model_list": [
            {
                "model_name": alias,
                "litellm_params": litellm_params,
            }
        ]
    }
    try:
        litellm_cfg = get_litellm_config()
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{litellm_cfg.base_url}/config/update",
                json=payload,
                headers=build_litellm_headers(litellm_cfg),
            )
        if resp.status_code < 400:
            logger.info(
                "Cloud alias %r → %s pushed to LiteLLM via /config/update",
                alias,
                model_name,
            )
            return True
        text = resp.text[:500]
        raise RuntimeError(
            f"LiteLLM /config/update failed for alias {alias!r}: HTTP {resp.status_code} {text}"
        )
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Could not push cloud alias {alias!r} to LiteLLM: {exc}") from exc


async def _post_ollama_alias_update(
    alias: str,
    litellm_model_string: str,
    extra_body: dict[str, Any] | None = None,
) -> bool:
    """Push an Ollama alias update to LiteLLM /config/update in-memory (no api_key needed)."""
    litellm_params: dict[str, Any] = {
        "model": litellm_model_string,
        "api_base": "http://ollama:11434",
    }
    if extra_body is not None:
        litellm_params["extra_body"] = extra_body
    payload: dict[str, Any] = {
        "model_list": [
            {
                "model_name": alias,
                "litellm_params": litellm_params,
            }
        ]
    }
    litellm_cfg = get_litellm_config()
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{litellm_cfg.base_url}/config/update",
            json=payload,
            headers=build_litellm_headers(litellm_cfg),
        )
    if resp.status_code < 400:
        logger.info(
            "Ollama alias %r → %s pushed via /config/update (YAML is :ro)",
            alias,
            litellm_model_string,
        )
        return True
    text = resp.text[:300]
    raise RuntimeError(
        f"LiteLLM /config/update failed for alias {alias!r}: HTTP {resp.status_code} {text}"
    )


async def reload_litellm() -> bool:
    """Signal LiteLLM to reload its config.

    Attempts to call the internal reload endpoint. If that fails,
    the config change will still take effect on LiteLLM's next restart.
    """
    try:
        # A5: use shared helper instead of local _get_litellm_key
        litellm_cfg = get_litellm_config()
        async with httpx.AsyncClient(timeout=5.0) as client:
            # LiteLLM proxy supports config reload via internal API
            resp = await client.post(
                f"{litellm_cfg.base_url}/config/update",
                json={},
                headers=build_litellm_headers(litellm_cfg),
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
