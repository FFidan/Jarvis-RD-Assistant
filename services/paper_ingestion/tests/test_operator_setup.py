"""Operator setup and Compose rendering regressions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_default_compose_config_renders_without_profile_env():
    """Profile-gated services must not require env vars during default config rendering."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")

    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_versions_env_compose_config_renders_without_letsencrypt_env():
    """Loading image pins must not require LetsEncrypt variables unless the profile runs."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")

    result = subprocess.run(
        ["docker", "compose", "--env-file", "versions.env", "config", "--format", "json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_bootstrap_scripts_load_generated_env_and_version_pins():
    """Bootstrap wrappers should not replace generated .env with versions.env."""
    sh_text = (REPO_ROOT / "scripts/jarvis-setup.sh").read_text()
    ps_text = (REPO_ROOT / "scripts/jarvis-setup.ps1").read_text()

    assert "--env-file .env" in sh_text
    assert "--env-file versions.env" in sh_text
    assert "'--env-file', '.env'" in ps_text
    assert "'--env-file', 'versions.env'" in ps_text


def test_bootstrap_scripts_probe_direct_http_dashboard_url():
    """The bootstrap path starts direct dashboard HTTP, not Caddy-local HTTPS."""
    sh_text = (REPO_ROOT / "scripts/jarvis-setup.sh").read_text()
    ps_text = (REPO_ROOT / "scripts/jarvis-setup.ps1").read_text()

    assert "http://localhost:${DASHBOARD_HOST_PORT}" in sh_text
    assert "https://localhost:3001/healthz" not in sh_text
    assert "http://localhost:$dashboardPort/" in ps_text
    assert "https://localhost:3001/healthz" not in ps_text
