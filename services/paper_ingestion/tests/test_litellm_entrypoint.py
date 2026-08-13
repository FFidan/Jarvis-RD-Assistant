"""Verify LiteLLM's secret-loading and production-startup configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _litellm_service() -> dict:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["litellm"]


def _litellm_entrypoint_source() -> str:
    return (REPO_ROOT / "scripts" / "litellm-entrypoint.sh").read_text(encoding="utf-8")


def test_compose_uses_shared_litellm_entrypoint() -> None:
    """Compose must invoke the repository's marker-aware LiteLLM entrypoint."""
    assert _litellm_service()["entrypoint"] == [
        "sh",
        "/usr/local/bin/litellm-entrypoint.sh",
    ]
    assert not (REPO_ROOT / "litellm" / "entrypoint.sh").exists()
    volumes = _litellm_service()["volumes"]
    assert "./litellm/pinned_launcher.py:/app/pinned_launcher.py:ro" in volumes
    assert (
        "./libs/jarvis_common/jarvis_common/pinned_transport.py:"
        "/app/jarvis_common/pinned_transport.py:ro" in volumes
    )
    assert "./libs/jarvis_common/jarvis_common/net.py:/app/jarvis_common/net.py:ro" in volumes


def test_litellm_launcher_fails_closed_without_the_reviewed_transport_hook() -> None:
    """The custom-provider path must not silently fall back to aiohttp or DNS."""
    launcher = (REPO_ROOT / "litellm" / "pinned_launcher.py").read_text(encoding="utf-8")
    assert '"aclient_session"' in launcher
    assert 'setattr(litellm, "disable_aiohttp_transport", True)' in launcher
    assert "PinnedAsyncTransport" in launcher
    assert "Unsupported LiteLLM transport contract" in launcher
    assert "custom-provider transport hook is unavailable" in launcher


def test_litellm_config_requires_master_key() -> None:
    """litellm/config.yaml must contain an active master_key in general_settings.

    The config supports master_key: os.environ/LITELLM_MASTER_KEY
    to gate LiteLLM admin endpoints (/model/new, /model/delete, /v1/model/info,
    etc.). Loopback binding is defence-in-depth; master_key is the second layer.
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
        "protect LiteLLM admin endpoints."
    )
    assert "os.environ/LITELLM_MASTER_KEY" in uncommented, (
        "litellm/config.yaml master_key must be sourced from the environment via "
        "'os.environ/LITELLM_MASTER_KEY' (not a hard-coded value)."
    )


def test_litellm_entrypoint_builds_database_url_from_secret() -> None:
    """Build ``DATABASE_URL`` inside the container from its mounted secret.

    A compose ``environment:`` entry would expose the postgres password via
    ``docker inspect``. The URL must target the dedicated
    ``litellm`` database, not the jarvis application database.

    Both the username and password are percent-encoded via python3/urllib.parse
    so that RFC-3986 reserved chars (@ : / # ? & %) in credentials do not
    silently corrupt the URL.
    """
    entrypoint = _litellm_entrypoint_source()
    assert 'export DATABASE_URL="postgresql://' in entrypoint, (
        "LiteLLM's entrypoint must export the Prisma database URL"
    )
    assert 'postgres_password_file="${secret_dir}/postgres_password"' in entrypoint, (
        "the database password must come from the mounted secret"
    )
    assert "@postgres:5432/litellm" in entrypoint, (
        "DATABASE_URL must target the dedicated 'litellm' database"
    )
    # URL-encoding: both credentials must go through urllib.parse.quote before
    # being embedded in the URL so reserved chars cannot corrupt the authority.
    assert "urllib.parse.quote" in entrypoint, (
        "LiteLLM's entrypoint must URL-encode database credentials "
        "to handle passwords containing RFC-3986 reserved chars"
    )
    assert "postgres_user_encoded" in entrypoint
    assert "postgres_password_encoded" in entrypoint
    environment = _litellm_service().get("environment") or {}
    assert "DATABASE_URL" not in environment, (
        "DATABASE_URL must not appear in LiteLLM's Compose environment map; "
        "docker inspect would leak the postgres password"
    )
    assert "postgres_password" in _litellm_service()["secrets"]


def test_litellm_entrypoint_exports_salt_key_from_secret() -> None:
    """Export the stable LiteLLM salt key from its dedicated secret.

    LiteLLM otherwise falls back to the master key for encryption, so rotating
    the master key could make stored credentials unreadable. The entrypoint
    must reject an empty salt-key file.
    """
    entrypoint = _litellm_entrypoint_source()
    assert 'LITELLM_SALT_KEY="$(cat "$salt_key_file")"' in entrypoint
    assert "export LITELLM_SALT_KEY" in entrypoint
    assert 'if [ ! -s "$salt_key_file" ]' in entrypoint, (
        "the entrypoint must refuse an empty or missing salt-key secret"
    )
    assert "litellm_salt_key" in _litellm_service()["secrets"]


def test_litellm_entrypoint_enforces_production_master_key_guards() -> None:
    """Reject missing, placeholder, and short production master keys."""
    entrypoint = _litellm_entrypoint_source()
    assert 'if [ ! -s "$master_key_file" ]' in entrypoint
    assert 'case "$LITELLM_MASTER_KEY" in' in entrypoint
    assert '"sk-jarvis-dev-test"|changeme|secret|password|""|"sk-1234")' in entrypoint
    assert "-lt 16" in entrypoint, "production minimum-length guard missing"
    assert 'litellm_launcher="/app/pinned_launcher.py"' in entrypoint
    assert 'python3 "$litellm_launcher" --config /app/config.yaml &' in entrypoint
    assert '"${ENVIRONMENT:-development}" != "test"' in entrypoint
