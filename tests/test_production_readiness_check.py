"""Tests for scripts/production-readiness-check.sh — weak-secret detection (H-5).

The script is invoked via subprocess so it behaves exactly as it would in CI.
Each test passes crafted environment variables and asserts exit code + output.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "production-readiness-check.sh"

# Strong test values that satisfy all length / non-placeholder checks.
_STRONG_API_KEY = "a" * 32
_STRONG_LITELLM = "str0ng-litellm-key-for-tests"
_STRONG_POSTGRES = "str0ng-postgres-pw!"


def _run(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the script with a clean env containing only the supplied variables."""
    env = {
        # PATH is needed for sh/bash builtins.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Unset .env loading side-effects by pointing HOME nowhere useful.
        "HOME": "/tmp",
    }
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Script sanity
# ---------------------------------------------------------------------------


def test_script_exists() -> None:
    assert _SCRIPT.is_file(), f"Script not found: {_SCRIPT}"


def test_script_syntax_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error:\n{result.stderr}"


# ---------------------------------------------------------------------------
# LITELLM_MASTER_KEY checks
# ---------------------------------------------------------------------------


def test_weak_litellm_key_fails_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": "sk-jarvis-dev-test",
            "POSTGRES_PASSWORD": _STRONG_POSTGRES,
        }
    )
    assert result.returncode == 1, "Expected exit 1 for placeholder LITELLM_MASTER_KEY"
    combined = result.stdout + result.stderr
    assert "LITELLM_MASTER_KEY" in combined
    assert "FAIL" in combined or "placeholder" in combined.lower() or "weak" in combined.lower()


def test_short_litellm_key_fails_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": "tooshort",  # < 16 chars
            "POSTGRES_PASSWORD": _STRONG_POSTGRES,
        }
    )
    assert result.returncode == 1, "Expected exit 1 for short LITELLM_MASTER_KEY"
    combined = result.stdout + result.stderr
    assert "LITELLM_MASTER_KEY" in combined


def test_strong_litellm_key_passes_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": _STRONG_LITELLM,
            "POSTGRES_PASSWORD": _STRONG_POSTGRES,
        }
    )
    # No HIGH failures expected from LITELLM_MASTER_KEY with strong value.
    # (Other checks may still fail if .env is missing; we only assert the key row is OK.)
    combined = result.stdout + result.stderr
    assert "Placeholder" not in combined or "LITELLM_MASTER_KEY" not in combined


def test_weak_litellm_key_warns_in_dev() -> None:
    result = _run(
        {
            "ENVIRONMENT": "development",
            "LITELLM_MASTER_KEY": "sk-jarvis-dev-test",
            "POSTGRES_PASSWORD": _STRONG_POSTGRES,
        }
    )
    # Should NOT exit 1 in development.
    assert result.returncode == 0, "Expected exit 0 for placeholder LITELLM_MASTER_KEY in dev"


# ---------------------------------------------------------------------------
# POSTGRES_PASSWORD checks
# ---------------------------------------------------------------------------


def test_weak_postgres_password_fails_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": _STRONG_LITELLM,
            "POSTGRES_PASSWORD": "jarvis_dev",
        }
    )
    assert result.returncode == 1, "Expected exit 1 for placeholder POSTGRES_PASSWORD"
    combined = result.stdout + result.stderr
    assert "POSTGRES_PASSWORD" in combined
    assert "FAIL" in combined or "placeholder" in combined.lower()


def test_changeme_postgres_password_fails_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": _STRONG_LITELLM,
            "POSTGRES_PASSWORD": "changeme",
        }
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "POSTGRES_PASSWORD" in combined


def test_short_postgres_password_fails_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": _STRONG_LITELLM,
            "POSTGRES_PASSWORD": "short",  # < 12 chars
        }
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "POSTGRES_PASSWORD" in combined


def test_strong_postgres_password_passes_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": _STRONG_LITELLM,
            "POSTGRES_PASSWORD": _STRONG_POSTGRES,
        }
    )
    combined = result.stdout + result.stderr
    # POSTGRES_PASSWORD row should not be FAIL.
    for line in combined.splitlines():
        if "POSTGRES_PASSWORD" in line:
            assert "FAIL" not in line, f"Unexpected FAIL for strong password: {line}"


def test_weak_postgres_password_warns_in_dev() -> None:
    result = _run(
        {
            "ENVIRONMENT": "development",
            "POSTGRES_PASSWORD": "jarvis_dev",
            "LITELLM_MASTER_KEY": _STRONG_LITELLM,
        }
    )
    assert result.returncode == 0, "Expected exit 0 for weak POSTGRES_PASSWORD in dev"


# ---------------------------------------------------------------------------
# Both weak at once — both must be reported
# ---------------------------------------------------------------------------


def test_both_weak_secrets_both_reported_in_production() -> None:
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": "sk-jarvis-dev-test",
            "POSTGRES_PASSWORD": "jarvis_dev",
        }
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "LITELLM_MASTER_KEY" in combined
    assert "POSTGRES_PASSWORD" in combined
