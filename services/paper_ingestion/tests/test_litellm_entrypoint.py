"""Tests verifying that the LiteLLM transparent-proxy configuration is in place.

litellm/entrypoint.sh was deleted and master_key removed
from litellm/config.yaml because litellm is loopback-only and needs no auth.

The compose ``sh -c`` shim is litellm's de-facto entrypoint, so its contracts
(secret-sourced DATABASE_URL / LITELLM_SALT_KEY, production master-key guards)
are pinned here too.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _litellm_service() -> dict:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["litellm"]


def _litellm_shim() -> str:
    command = _litellm_service()["command"]
    assert isinstance(command, list) and len(command) == 1, (
        "litellm's command must stay a single sh -c shim string"
    )
    return command[0]


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
        "gate LiteLLM admin endpoints (Group C security hardening)."
    )
    assert "os.environ/LITELLM_MASTER_KEY" in uncommented, (
        "litellm/config.yaml master_key must be sourced from the environment via "
        "'os.environ/LITELLM_MASTER_KEY' (not a hard-coded value)."
    )


def test_litellm_shim_builds_database_url_from_secret() -> None:
    """DATABASE_URL is built inside the shim from the Docker Secret, never in env.

    A compose ``environment:`` entry would expose the postgres password via
    ``docker inspect`` (SEC-002 reasoning). The URL must target the dedicated
    ``litellm`` database, not the jarvis application database.

    Both the username and password are percent-encoded via python3/urllib.parse
    so that RFC-3986 reserved chars (@ : / # ? & %) in credentials do not
    silently corrupt the URL.
    """
    shim = _litellm_shim()
    assert 'export DATABASE_URL="postgresql://' in shim, (
        "litellm shim must export DATABASE_URL (prisma needs it to attach the admin DB)"
    )
    assert "$(cat /run/secrets/postgres_password)" in shim, (
        "DATABASE_URL's password must be read from /run/secrets/postgres_password "
        "at container start, not interpolated by compose"
    )
    assert "@postgres:5432/litellm" in shim, (
        "DATABASE_URL must target the dedicated 'litellm' database"
    )
    # URL-encoding: both credentials must go through urllib.parse.quote before
    # being embedded in the URL so reserved chars cannot corrupt the authority.
    assert "urllib.parse.quote" in shim, (
        "litellm shim must URL-encode DB credentials via urllib.parse.quote "
        "to handle passwords containing RFC-3986 reserved chars"
    )
    assert "PG_USER_ENC" in shim, "litellm shim must store the URL-encoded username in PG_USER_ENC"
    assert "PG_PASS_ENC" in shim, "litellm shim must store the URL-encoded password in PG_PASS_ENC"
    environment = _litellm_service().get("environment") or {}
    assert "DATABASE_URL" not in environment, (
        "DATABASE_URL must NOT appear in litellm's compose environment map — "
        "docker inspect would leak the postgres password"
    )
    assert "postgres_password" in _litellm_service()["secrets"]


def test_litellm_shim_exports_salt_key_from_secret() -> None:
    """LITELLM_SALT_KEY is pinned as its own secret and exported in the shim.

    litellm's salt-key fallback is the master key; rotating the master key
    would then brick DB-stored encrypted credentials. The shim must fail fast
    when the secret file is empty, like the master-key guard.
    """
    shim = _litellm_shim()
    assert 'export LITELLM_SALT_KEY="$(cat /run/secrets/litellm_salt_key)"' in shim
    assert "if [ ! -s /run/secrets/litellm_salt_key ]" in shim, (
        "shim must refuse to start when the salt-key secret file is empty/missing"
    )
    assert "litellm_salt_key" in _litellm_service()["secrets"]


def test_litellm_shim_production_guards_intact() -> None:
    """The placeholder/weak-master-key production guards must survive DB wiring."""
    shim = _litellm_shim()
    assert "if [ ! -s /run/secrets/litellm_master_key ]" in shim
    assert 'case "$LITELLM_MASTER_KEY" in' in shim
    assert '"sk-jarvis-dev-test"|changeme|secret|password|""|"sk-1234")' in shim
    assert "-lt 16" in shim, "production minimum-length guard missing"
    exec_line = "exec litellm --config /app/config.yaml"
    assert exec_line in shim
    # DATABASE_URL/SALT_KEY exports must happen before the exec hands off.
    assert shim.index('export DATABASE_URL="postgresql://') < shim.index(exec_line)
    assert shim.index("export LITELLM_SALT_KEY=") < shim.index(exec_line)
