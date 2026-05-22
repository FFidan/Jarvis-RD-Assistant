"""Pre-design Ollama-only .env survives upgrade — no surprise mutations."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.getenv("SMOKE_INTEGRATION") != "1", reason="integration gated")
def test_existing_env_keys_untouched(tmp_path):
    env_path = tmp_path / ".env"
    pre_design_env = (
        "JARVIS_API_KEY=test-key\nJARVIS_CONFIG_KEY=test-config\nJARVIS_SETUP_MODE=single\n"
    )
    env_path.write_text(pre_design_env)
    # Simulate "upgrade" — copy current setup.sh into tmpdir and run --check
    shutil.copy(REPO_ROOT / "setup.sh", tmp_path / "setup.sh")
    shutil.copy(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    result = subprocess.run(
        ["bash", "setup.sh", "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode in (0, 1)
    # .env is unchanged after --check (read-only doctor)
    assert env_path.read_text() == pre_design_env
