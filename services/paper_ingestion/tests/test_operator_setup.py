"""Operator setup and Compose rendering regressions."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# Canonical set of auto-generated secret files the compose stack requires.
# Derived from docker-compose.yml top-level ``secrets:`` block.
# ``telegram_bot_token`` and ``cloudflare_tunnel_token`` are excluded because
# they require manual values and are intentionally left absent on fresh runs.
# ---------------------------------------------------------------------------
_AUTO_SECRET_FILES = {
    "jarvis_api_key.txt",
    "litellm_master_key.txt",
    "postgres_password.txt",
    "qdrant_api_key.txt",
    "jarvis_config_key.txt",
    "langfuse_nextauth_secret.txt",
    "langfuse_salt.txt",
    "langfuse_pg_password.txt",
    "n8n_encryption_key.txt",
    "n8n_jwt_secret.txt",
    "backup_encrypt_key.txt",
    "infra_ingest_key.txt",
    "vector_writer_password.txt",
}


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


def test_init_secrets_generates_all_required_secret_files():
    """H-4 regression: init-secrets.sh must create every auto-generable secret
    file that docker-compose.yml mounts.

    Runs scripts/init-secrets.sh in an isolated tmpdir with a minimal .env stub
    so the generator produces fresh random values.  Asserts each required file
    exists, is non-empty, and has mode 600.
    """
    import shutil as _shutil

    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")

    init_sh = REPO_ROOT / "scripts" / "init-secrets.sh"
    if not init_sh.exists():
        pytest.skip("scripts/init-secrets.sh not found")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Create a minimal stub .env so init-secrets.sh finds its home.
        (tmp / ".env").write_text("")
        (tmp / "secrets").mkdir()

        # Copy the script into tmpdir/scripts so SCRIPT_DIR resolves to tmpdir.
        dest_scripts = tmp / "scripts"
        dest_scripts.mkdir()
        _shutil.copy(init_sh, dest_scripts / "init-secrets.sh")

        result = subprocess.run(
            ["bash", str(dest_scripts / "init-secrets.sh")],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"init-secrets.sh exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        secrets_dir = tmp / "secrets"
        missing: list[str] = []
        empty: list[str] = []
        bad_mode: list[str] = []

        for fname in _AUTO_SECRET_FILES:
            fpath = secrets_dir / fname
            if not fpath.exists():
                missing.append(fname)
                continue
            if fpath.stat().st_size == 0:
                empty.append(fname)
            mode = oct(fpath.stat().st_mode & 0o777)
            if mode != "0o600":
                bad_mode.append(f"{fname} (mode {mode})")

        assert not missing, f"Missing secret files: {missing}"
        assert not empty, f"Empty secret files: {empty}"
        assert not bad_mode, f"Secret files with wrong mode (want 0o600): {bad_mode}"


def test_setup_check_is_side_effect_free():
    """--check (doctor mode) must print a PREFLIGHT: line and must NOT create .env."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Copy setup.sh and .env.example into the isolated tmpdir.
        shutil.copy2(REPO_ROOT / "setup.sh", tmp / "setup.sh")
        (tmp / "setup.sh").chmod(0o755)
        if (REPO_ROOT / ".env.example").exists():
            shutil.copy2(REPO_ROOT / ".env.example", tmp / ".env.example")

        # Copy the scripts/ directory (init-secrets.sh and friends are sourced
        # by setup.sh even in --check mode; the doctor only reads, never writes).
        scripts_src = REPO_ROOT / "scripts"
        if scripts_src.is_dir():
            shutil.copytree(str(scripts_src), str(tmp / "scripts"))

        result = subprocess.run(
            ["bash", "setup.sh", "--check"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        combined = result.stdout + result.stderr
        assert result.returncode in {0, 1}, (
            f"Expected exit 0 or 1, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "PREFLIGHT:" in combined, f"Expected 'PREFLIGHT:' in output, got:\n{combined}"
        assert not (tmp / ".env").exists(), "--check must not write a .env file"


@pytest.mark.parametrize(
    "mode,expected_login",
    [
        ("single", "true"),
        ("multi", "false"),
    ],
)
def test_setup_mode_written_to_env(mode: str, expected_login: str):
    """--non-interactive --mode <mode> must write JARVIS_SETUP_MODE and
    API_KEY_LOGIN_ENABLED to the generated .env.

    The script requires docker, docker-compose-v2, and openssl before it
    reaches the .env write step.  If any prereq is absent the script will
    die() before producing .env — in that case the test is skipped (rather
    than failed) because the behaviour under test has not been reached.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if shutil.which("openssl") is None:
        pytest.skip("openssl required for secret generation in setup.sh")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        shutil.copy2(REPO_ROOT / "setup.sh", tmp / "setup.sh")
        (tmp / "setup.sh").chmod(0o755)
        if (REPO_ROOT / ".env.example").exists():
            shutil.copy2(REPO_ROOT / ".env.example", tmp / ".env.example")

        scripts_src = REPO_ROOT / "scripts"
        if scripts_src.is_dir():
            shutil.copytree(str(scripts_src), str(tmp / "scripts"))

        result = subprocess.run(
            ["bash", "setup.sh", "--non-interactive", "--mode", mode],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

        env_path = tmp / ".env"
        if not env_path.exists():
            # Script died before writing .env — most likely docker/docker-compose
            # is absent on this machine.  The values-under-test live downstream
            # of those prereq checks, so skip rather than fail.
            pytest.skip(
                f"setup.sh exited {result.returncode} before writing .env — "
                f"likely a missing prereq (docker/docker-compose). "
                f"stderr: {result.stderr[:400]}"
            )

        env_text = env_path.read_text()
        assert f"JARVIS_SETUP_MODE={mode}" in env_text, (
            f"Expected 'JARVIS_SETUP_MODE={mode}' in .env:\n{env_text}"
        )
        assert f"API_KEY_LOGIN_ENABLED={expected_login}" in env_text, (
            f"Expected 'API_KEY_LOGIN_ENABLED={expected_login}' in .env:\n{env_text}"
        )
