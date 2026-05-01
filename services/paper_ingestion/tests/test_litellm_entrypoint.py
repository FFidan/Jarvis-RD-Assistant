"""Tests verifying that the LiteLLM transparent-proxy configuration is in place.

Wave 1, Round-15 audit: litellm/entrypoint.sh was deleted and master_key removed
from litellm/config.yaml because litellm is loopback-only and needs no auth.
"""

from __future__ import annotations

from pathlib import Path


def test_litellm_entrypoint_deleted() -> None:
    """litellm/entrypoint.sh must not exist (transparent proxy, no auth required)."""
    repo_root = Path(__file__).resolve().parents[3]
    entrypoint = repo_root / "litellm" / "entrypoint.sh"
    assert not entrypoint.exists(), (
        "litellm/entrypoint.sh still exists but was removed in Wave 1 of the "
        "Round-15 audit.  Delete it and remove the entrypoint: reference from "
        "docker-compose.yml to complete the transparent-proxy migration."
    )


def test_litellm_config_has_no_master_key() -> None:
    """litellm/config.yaml must not contain an active master_key setting."""
    import re

    repo_root = Path(__file__).resolve().parents[3]
    config_path = repo_root / "litellm" / "config.yaml"
    assert config_path.exists(), "litellm/config.yaml not found"
    content = config_path.read_text(encoding="utf-8")
    # Strip comments, then check no `master_key:` setting remains.
    uncommented = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"\bmaster_key\s*:", uncommented), (
        "litellm/config.yaml still contains an active master_key setting.  Remove it "
        "to complete the transparent-proxy migration (Wave 1, Round-15 audit)."
    )
