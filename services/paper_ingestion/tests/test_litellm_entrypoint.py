"""Tests for the LiteLLM Docker entrypoint shell script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_litellm_entrypoint_execs_binary_with_compose_args(tmp_path: Path) -> None:
    """Entrypoint must call ``litellm "$@"`` so compose command args are preserved."""
    repo_root = Path(__file__).resolve().parents[3]
    entrypoint = repo_root / "litellm" / "entrypoint.sh"

    secret_file = tmp_path / "litellm_master_key"
    secret_file.write_text("test-master-key\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "litellm"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s" "$LITELLM_MASTER_KEY" > "$JARVIS_STUB_OUT/master_key"\n'
        'printf "%s\\n" "$@" > "$JARVIS_STUB_OUT/args"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "JARVIS_STUB_OUT": str(tmp_path),
        "LITELLM_MASTER_KEY_FILE": str(secret_file),
    }
    subprocess.run(
        ["/bin/sh", str(entrypoint), "--config", "/app/config.yaml"],
        env=env,
        check=True,
        timeout=30,
    )

    assert (tmp_path / "master_key").read_text(encoding="utf-8") == "test-master-key"
    assert (tmp_path / "args").read_text(encoding="utf-8").splitlines() == [
        "--config",
        "/app/config.yaml",
    ]
