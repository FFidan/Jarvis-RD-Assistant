"""Regression tests for scripts/init-secrets.sh idempotent bootstrap.

A fresh ``cp .env.example .env`` leaves the core secret keys as EMPTY
placeholders. The bootstrapper must fill them in
place — no duplicate lines — and write the matching ``secrets/*.txt`` files that
``docker-compose.yml`` mounts as Docker secrets. The earlier readback-based
writer appended a second ``KEY=value`` line and then read back the *empty*
placeholder (first match), silently skipping the secret file and breaking
``docker compose up`` for the core keys.

Runs the real script in an isolated temp dir (never touches the repo's .env).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "init-secrets.sh"
SECRET_DIR_MODE = 0o700
SECRET_FILE_MODE = 0o644

# The "core" keys that ship as empty placeholders in .env.example and are
# consumed by docker-compose.yml as `_FILE` Docker secrets.
CORE_KEYS = {
    "JARVIS_API_KEY": "jarvis_api_key.txt",
    "QDRANT_API_KEY": "qdrant_api_key.txt",
    "JARVIS_CONFIG_KEY": "jarvis_config_key.txt",
    "JARVIS_MODEL_HMAC_KEY": "jarvis_model_hmac_key.txt",
    "LITELLM_MASTER_KEY": "litellm_master_key.txt",
    "LANGFUSE_INIT_USER_PASSWORD": "langfuse_init_user_password.txt",
}

DATABASE_PASSWORD_FILES = {
    "postgres_password.txt",
    "postgres_platform_runtime_password.txt",
    "postgres_research_runtime_password.txt",
    "postgres_learning_runtime_password.txt",
    "postgres_migrator_password.txt",
    "postgres_cluster_bootstrap_password.txt",
    "postgres_backup_reader_password.txt",
    "postgres_restore_operator_password.txt",
    "postgres_erasure_executor_password.txt",
    "litellm_runtime_password.txt",
    "litellm_migrator_password.txt",
}

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None or shutil.which("bash") is None,
    reason="init-secrets.sh needs bash + openssl",
)


def _stage(tmp: Path, env_body: str) -> None:
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tmp / "scripts" / "init-secrets.sh")
    (tmp / ".env").write_text(env_body)


def _run(tmp: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/init-secrets.sh"],
        cwd=str(tmp),
        capture_output=True,
        text=True,
        env=env,
    )


def _count_lines(env_path: Path, key: str) -> int:
    return sum(1 for ln in env_path.read_text().splitlines() if ln.startswith(f"{key}="))


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_fills_empty_placeholders_and_writes_secret_files(tmp_path: Path) -> None:
    # Simulate `cp .env.example .env`: every core key present but EMPTY.
    _stage(tmp_path, "# core secrets\n" + "".join(f"{k}=\n" for k in CORE_KEYS))

    result = _run(tmp_path)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert _mode(tmp_path / "secrets") == SECRET_DIR_MODE
    for key, filename in CORE_KEYS.items():
        secret_file = tmp_path / "secrets" / filename
        assert secret_file.exists() and secret_file.read_text().strip(), (
            f"{filename} missing/empty for placeholder key {key}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert _mode(secret_file) == SECRET_FILE_MODE
        # The placeholder must be filled IN PLACE — exactly one line, non-empty.
        assert _count_lines(tmp_path / ".env", key) == 1, (
            f"{key} appears more than once in .env (duplicate-append bug):\n"
            f"{(tmp_path / '.env').read_text()}"
        )
    for filename in DATABASE_PASSWORD_FILES:
        secret_file = tmp_path / "secrets" / filename
        assert secret_file.exists() and secret_file.read_text().strip()
        assert _mode(secret_file) == SECRET_FILE_MODE


def test_database_password_files_are_preserved_without_env_copies(tmp_path: Path) -> None:
    """Database login passwords remain file-only and stable across reruns."""
    _stage(tmp_path, "")
    first = _run(tmp_path)
    assert first.returncode == 0, first.stderr
    before = {
        filename: (tmp_path / "secrets" / filename).read_text()
        for filename in DATABASE_PASSWORD_FILES
    }

    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr
    assert {
        filename: (tmp_path / "secrets" / filename).read_text()
        for filename in DATABASE_PASSWORD_FILES
    } == before
    env_text = (tmp_path / ".env").read_text()
    assert "PLATFORM_RUNTIME_PASSWORD=" not in env_text
    assert "LITELLM_MIGRATOR_PASSWORD=" not in env_text


def test_legacy_postgres_env_password_moves_to_isolated_upgrade_file(tmp_path: Path) -> None:
    """An existing v1.2.5 password remains usable without staying in ``.env``."""
    _stage(tmp_path, "POSTGRES_PASSWORD=existing-v125-password\n")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "secrets" / "postgres_password.txt").read_text() == (
        "existing-v125-password"
    )
    assert "POSTGRES_PASSWORD=" not in (tmp_path / ".env").read_text()
    assert not (tmp_path / "secrets" / "postgres_legacy_rollback_password.txt").exists()


def test_is_idempotent_and_preserves_existing_values(tmp_path: Path) -> None:
    _stage(tmp_path, "JARVIS_CONFIG_KEY=\n")

    first = _run(tmp_path)
    assert first.returncode == 0, first.stderr
    cfg1 = (tmp_path / "secrets" / "jarvis_config_key.txt").read_text()

    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr

    # Values must be preserved across runs (rotating JARVIS_CONFIG_KEY would
    # render every encrypted user_config row unreadable).
    assert (tmp_path / "secrets" / "jarvis_config_key.txt").read_text() == cfg1
    assert _mode(tmp_path / "secrets") == SECRET_DIR_MODE
    assert _mode(tmp_path / "secrets" / "jarvis_config_key.txt") == SECRET_FILE_MODE
    for key in ("JARVIS_CONFIG_KEY",):
        assert _count_lines(tmp_path / ".env", key) == 1, "duplicate KEY= line after re-run"


def test_restored_data_keys_are_file_authoritative_but_host_credentials_are_not(
    tmp_path: Path,
) -> None:
    _stage(
        tmp_path,
        "JARVIS_CONFIG_KEY=stale-config-env\n"
        "JARVIS_MODEL_HMAC_KEY=stale-hmac-env-value-that-is-long-enough\n"
        "LITELLM_SALT_KEY=stale-salt-env\n",
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    restored = {
        "JARVIS_CONFIG_KEY": ("jarvis_config_key.txt", "restored-config-file"),
        "JARVIS_MODEL_HMAC_KEY": (
            "jarvis_model_hmac_key.txt",
            "restored-hmac-file-value-that-is-long-enough",
        ),
        "LITELLM_SALT_KEY": ("litellm_salt_key.txt", "restored-salt-file"),
    }
    for filename, value in restored.values():
        (secrets / filename).write_text(value)

    result = _run(tmp_path)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    env_values = dict(
        line.split("=", 1)
        for line in (tmp_path / ".env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    for key, (filename, value) in restored.items():
        assert env_values[key] == value
        assert (secrets / filename).read_text() == value


def test_restored_data_key_validation_does_not_require_gnu_stat(tmp_path: Path) -> None:
    _stage(tmp_path, "LITELLM_SALT_KEY=stale-env-value\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "litellm_salt_key.txt").write_text("restored-portable-key")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stat_stub = stub_bin / "stat"
    stat_stub.write_text("#!/usr/bin/env bash\nexit 99\n")
    stat_stub.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"

    result = _run(tmp_path, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "LITELLM_SALT_KEY=restored-portable-key" in (tmp_path / ".env").read_text()


@pytest.mark.parametrize("kind", ["symlink", "directory", "empty", "oversized"])
def test_invalid_restored_data_key_files_fail_closed(tmp_path: Path, kind: str) -> None:
    _stage(tmp_path, "LITELLM_SALT_KEY=stale-env-value\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key_file = secrets / "litellm_salt_key.txt"
    if kind == "symlink":
        target = tmp_path / "outside-key"
        target.write_text("do-not-import")
        key_file.symlink_to(target)
    elif kind == "directory":
        key_file.mkdir()
    elif kind == "empty":
        key_file.touch()
    else:
        key_file.write_bytes(b"x" * 4097)

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "refusing" in result.stderr.lower() or "no usable data key" in result.stderr.lower()
    assert "LITELLM_SALT_KEY=stale-env-value" in (tmp_path / ".env").read_text()
