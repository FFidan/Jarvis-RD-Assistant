"""A0-4 — check-no-tracked-secrets.sh correctness tests.

Verifies that the script exits non-zero when a secrets/*.txt file is staged
and exits zero when no such files are tracked.  Uses an isolated temporary git
repository so real repo state is never mutated.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-no-tracked-secrets.sh"


def _init_tmp_repo(tmp: Path) -> Path:
    """Create a minimal git repo in *tmp* with a secrets/ directory."""
    subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    secrets_dir = tmp / "secrets"
    secrets_dir.mkdir()
    # Always copy the script so it can resolve relative paths correctly.
    scripts_dir = tmp / "scripts"
    scripts_dir.mkdir()
    shutil.copy(CHECK_SCRIPT, scripts_dir / "check-no-tracked-secrets.sh")
    return secrets_dir


def _run_check(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "scripts/check-no-tracked-secrets.sh"],
        cwd=str(tmp),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


@pytest.fixture
def tmp_repo(tmp_path):
    """A fresh isolated git repo for each test."""
    return tmp_path


def test_check_exits_zero_when_no_txt_tracked(tmp_repo):
    """Script exits 0 when secrets/ has no .txt files staged."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if shutil.which("git") is None:
        pytest.skip("git not available")

    _init_tmp_repo(tmp_repo)

    result = _run_check(tmp_repo)
    assert result.returncode == 0, (
        f"Expected exit 0 with no tracked secrets.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_check_exits_nonzero_when_txt_is_staged(tmp_repo):
    """Script exits 1 when a secrets/*.txt file is staged via git add."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if shutil.which("git") is None:
        pytest.skip("git not available")

    secrets_dir = _init_tmp_repo(tmp_repo)

    # Create and stage a credential file.
    secret_file = secrets_dir / "some_api_key.txt"
    secret_file.write_text("supersecret")
    subprocess.run(
        ["git", "add", str(secret_file)],
        cwd=str(tmp_repo),
        check=True,
        capture_output=True,
    )

    result = _run_check(tmp_repo)
    assert result.returncode != 0, (
        f"Expected non-zero exit when secrets/*.txt is staged.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ERROR" in result.stderr
    assert "some_api_key.txt" in result.stderr


def test_check_allows_gitkeep_and_readme(tmp_repo):
    """secrets/.gitkeep and secrets/README.md are allowed to be tracked."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if shutil.which("git") is None:
        pytest.skip("git not available")

    secrets_dir = _init_tmp_repo(tmp_repo)

    # .gitkeep and README.md are not .txt files — the script only checks *.txt.
    (secrets_dir / ".gitkeep").write_text("")
    (secrets_dir / "README.md").write_text("# secrets\n")

    result = _run_check(tmp_repo)
    assert result.returncode == 0, (
        f"Expected exit 0 — .gitkeep and README.md are not .txt files.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
