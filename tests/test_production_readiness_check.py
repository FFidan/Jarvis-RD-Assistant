"""Tests for scripts/production-readiness-check.sh — weak-secret detection (H-5).

The script is invoked via subprocess so it behaves exactly as it would in CI.
Each test passes crafted environment variables and asserts exit code + output.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
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
    # A weak secret in dev is a WARN, not a HIGH: exit 2 (warnings present), never 1.
    assert result.returncode == 2, (
        "Expected exit 2 (warn, not HIGH) for placeholder LITELLM_MASTER_KEY in dev"
    )


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
    # A weak secret in dev is a WARN, not a HIGH: exit 2 (warnings present), never 1.
    assert result.returncode == 2, (
        "Expected exit 2 (warn, not HIGH) for weak POSTGRES_PASSWORD in dev"
    )


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


# ---------------------------------------------------------------------------
# HTTPS row — real probe when a Let's Encrypt domain is configured
# ---------------------------------------------------------------------------


def _run_with_stub_curl(
    env_overrides: dict[str, str], curl_exit: int, curl_stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run the script with a fake ``curl`` on PATH that exits ``curl_exit``.

    The stub ignores its arguments, writes ``curl_stderr`` to stderr, and exits
    with the requested status, so a test drives the HTTPS probe outcome without
    any network access.
    """
    stub_dir = tempfile.mkdtemp()
    curl_path = Path(stub_dir) / "curl"
    curl_path.write_text(
        f"#!/usr/bin/env bash\nprintf '%s' {shlex.quote(curl_stderr)} >&2\nexit {int(curl_exit)}\n"
    )
    curl_path.chmod(0o755)
    real_path = os.environ.get("PATH", "/usr/bin:/bin")
    overrides = dict(env_overrides)
    overrides["PATH"] = f"{stub_dir}{os.pathsep}{real_path}"
    return _run(overrides)


def _row(output: str, name: str) -> str:
    """Return the summary-table row whose CHECK column starts with ``name``."""
    for line in output.splitlines():
        if line.startswith(name):
            return line
    raise AssertionError(f"No {name!r} row found in:\n{output}")


def test_https_probe_failure_warns() -> None:
    result = _run_with_stub_curl(
        {"ENVIRONMENT": "production", "LETSENCRYPT_DOMAIN": "example.test"},
        curl_exit=7,
        curl_stderr="curl: (7) Failed to connect to example.test port 443",
    )
    row = _row(result.stdout, "HTTPS")
    assert "WARN" in row, f"Expected HTTPS WARN on probe failure, got: {row}"
    assert "probe" in row.lower(), f"Expected probe-derived WARN, got: {row}"
    assert "Failed to connect" in row, f"Expected the curl failure named, got: {row}"


def test_https_probe_success_is_ok() -> None:
    result = _run_with_stub_curl(
        {"ENVIRONMENT": "production", "LETSENCRYPT_DOMAIN": "example.test"},
        curl_exit=0,
    )
    row = _row(result.stdout, "HTTPS")
    assert "OK" in row, f"Expected HTTPS OK on probe success, got: {row}"
    assert "WARN" not in row, f"Expected no WARN on probe success, got: {row}"
    assert "probe" in row.lower(), f"Expected probe-derived OK, got: {row}"


def test_cert_san_arm_removed() -> None:
    text = _SCRIPT.read_text()
    assert "JARVIS_CERT_SAN" not in text, "SAN string-match arm must be deleted"
    assert "Self-signed cert SAN" not in text


# ---------------------------------------------------------------------------
# SMTP row — environment presence, not a delivery-readiness claim
# ---------------------------------------------------------------------------


def test_smtp_row_reports_presence_not_stdout() -> None:
    result = _run(
        {
            "ENVIRONMENT": "development",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "jarvis@example.test",
        }
    )
    combined = result.stdout + result.stderr
    assert "environment SMTP presence" in combined, combined
    assert "GET /api/setup/status" in combined, combined
    assert "stdout" not in combined.lower(), "the stdout-delivery claim must be gone"
    # The false claim must not survive in the script source either.
    text = _SCRIPT.read_text()
    assert "logged to stdout" not in text
    assert "stdout" not in text


# ---------------------------------------------------------------------------
# Exit contract: 0 = clean, 2 = warnings present, 1 = HIGH issues
# ---------------------------------------------------------------------------


def test_warnings_present_exit_two() -> None:
    """A WARN-carrying run (no HIGH) exits 2, so callers can tell it from clean."""
    result = _run({"ENVIRONMENT": "development"})
    combined = result.stdout + result.stderr
    assert "WARN" in combined, "expected at least one WARN row in a bare dev run"
    assert result.returncode == 2, f"WARN-only run must exit 2, got {result.returncode}"


def test_clean_run_exits_zero() -> None:
    """A run with no WARN and no HIGH exits 0."""
    result = _run(
        {
            "ENVIRONMENT": "development",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": _STRONG_LITELLM,
            "POSTGRES_PASSWORD": _STRONG_POSTGRES,
            "QDRANT_API_KEY": _STRONG_LITELLM,
            "JARVIS_CONFIG_KEY": _STRONG_LITELLM,
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "jarvis@example.test",
        }
    )
    combined = result.stdout + result.stderr
    assert "WARN" not in combined, f"expected no WARN rows, got:\n{combined}"
    assert result.returncode == 0, f"clean run must exit 0, got {result.returncode}"


def test_high_issue_exits_one() -> None:
    """A HIGH issue (placeholder secret in production) exits 1."""
    result = _run(
        {
            "ENVIRONMENT": "production",
            "JARVIS_API_KEY": _STRONG_API_KEY,
            "LITELLM_MASTER_KEY": "changeme",  # placeholder -> HIGH in production
            "POSTGRES_PASSWORD": _STRONG_POSTGRES,
        }
    )
    assert result.returncode == 1, f"HIGH-issue run must exit 1, got {result.returncode}"
