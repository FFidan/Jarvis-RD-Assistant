"""Tests verifying that the LiteLLM transparent-proxy configuration is in place.

litellm/entrypoint.sh was deleted and master_key removed
from litellm/config.yaml because litellm is loopback-only and needs no auth.
"""

from __future__ import annotations

from pathlib import Path


def test_litellm_entrypoint_deleted() -> None:
    """litellm/entrypoint.sh must not exist (transparent proxy, no auth required)."""
    repo_root = Path(__file__).resolve().parents[3]
    entrypoint = repo_root / "litellm" / "entrypoint.sh"
    assert not entrypoint.exists(), (
        "litellm/entrypoint.sh still exists but was removed during the "
        "transparent-proxy migration.  Delete it and remove the entrypoint: reference from "
        "docker-compose.yml to complete the transparent-proxy migration."
    )


def test_litellm_config_requires_master_key() -> None:
    """litellm/config.yaml must contain an active master_key in general_settings.

    Group C (commit c84a7f9c) added master_key: os.environ/LITELLM_MASTER_KEY
    to gate LiteLLM admin endpoints (/config/update, /model/*, etc.).
    Loopback binding is defence-in-depth; master_key is the second layer.
    """
    import re

    repo_root = Path(__file__).resolve().parents[3]
    config_path = repo_root / "litellm" / "config.yaml"
    assert config_path.exists(), "litellm/config.yaml not found"
    content = config_path.read_text(encoding="utf-8")
    # Strip comments, then verify master_key is present and env-sourced.
    uncommented = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    )
    assert re.search(r"\bmaster_key\s*:", uncommented), (
        "litellm/config.yaml is missing an active master_key setting under "
        "general_settings.  Add 'master_key: os.environ/LITELLM_MASTER_KEY' to "
        "gate LiteLLM admin endpoints (Group C security hardening)."
    )
    assert "os.environ/LITELLM_MASTER_KEY" in uncommented, (
        "litellm/config.yaml master_key must be sourced from the environment via "
        "'os.environ/LITELLM_MASTER_KEY' (not a hard-coded value)."
    )
