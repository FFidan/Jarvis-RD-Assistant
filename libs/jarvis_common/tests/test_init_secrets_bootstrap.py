"""Regression tests for scripts/init-secrets.sh idempotent bootstrap.

A fresh ``cp .env.example .env`` leaves the core secret keys as EMPTY
placeholders (``POSTGRES_PASSWORD=`` etc.). The bootstrapper must fill them in
place — no duplicate lines — and write the matching ``secrets/*.txt`` files that
``docker-compose.yml`` mounts as Docker secrets. The earlier readback-based
writer appended a second ``KEY=value`` line and then read back the *empty*
placeholder (first match), silently skipping the secret file and breaking
``docker compose up`` for the core keys.

Runs the real script in an isolated temp dir (never touches the repo's .env).
"""

from __future__ import annotations

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
    "POSTGRES_PASSWORD": "postgres_password.txt",
    "JARVIS_API_KEY": "jarvis_api_key.txt",
    "QDRANT_API_KEY": "qdrant_api_key.txt",
    "JARVIS_CONFIG_KEY": "jarvis_config_key.txt",
    "JARVIS_MODEL_HMAC_KEY": "jarvis_model_hmac_key.txt",
    "LITELLM_MASTER_KEY": "litellm_master_key.txt",
    "LANGFUSE_INIT_USER_PASSWORD": "langfuse_init_user_password.txt",
}

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None or shutil.which("bash") is None,
    reason="init-secrets.sh needs bash + openssl",
)


def _stage(tmp: Path, env_body: str) -> None:
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tmp / "scripts" / "init-secrets.sh")
    (tmp / ".env").write_text(env_body)


def _run(tmp: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/init-secrets.sh"],
        cwd=str(tmp),
        capture_output=True,
        text=True,
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


def test_is_idempotent_and_preserves_existing_values(tmp_path: Path) -> None:
    _stage(tmp_path, "POSTGRES_PASSWORD=\nJARVIS_CONFIG_KEY=\n")

    first = _run(tmp_path)
    assert first.returncode == 0, first.stderr
    pw1 = (tmp_path / "secrets" / "postgres_password.txt").read_text()
    cfg1 = (tmp_path / "secrets" / "jarvis_config_key.txt").read_text()
    (tmp_path / "secrets" / "postgres_password.txt").chmod(0o600)

    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr

    # Values must be preserved across runs (rotating JARVIS_CONFIG_KEY would
    # render every encrypted user_config row unreadable).
    assert (tmp_path / "secrets" / "postgres_password.txt").read_text() == pw1
    assert (tmp_path / "secrets" / "jarvis_config_key.txt").read_text() == cfg1
    assert _mode(tmp_path / "secrets") == SECRET_DIR_MODE
    assert _mode(tmp_path / "secrets" / "postgres_password.txt") == SECRET_FILE_MODE
    assert _mode(tmp_path / "secrets" / "jarvis_config_key.txt") == SECRET_FILE_MODE
    for key in ("POSTGRES_PASSWORD", "JARVIS_CONFIG_KEY"):
        assert _count_lines(tmp_path / ".env", key) == 1, "duplicate KEY= line after re-run"
