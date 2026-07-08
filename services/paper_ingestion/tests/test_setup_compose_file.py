"""Unit tests for scripts/setup_lib.sh.

setup.sh persists the resolved compose-file set into .env (via COMPOSE_FILE) so a
later bare ``docker compose up`` re-engages the same overlays (notably the GPU
overlay) the installer used. The compute + idempotent-upsert helpers live in a
sourceable lib so they can be exercised directly in a bash subprocess — no real
GPU, no docker, fast and deterministic.

Runs the real lib in an isolated temp dir (never touches the repo's .env).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB = REPO_ROOT / "scripts" / "setup_lib.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="setup_lib.sh needs bash")


def test_infra_host_ports_are_overridable_for_isolated_smoke() -> None:
    """Infrastructure services should all support isolated smoke host ports.

    The first-run smoke runs beside normal local stacks, so services must not be
    pinned to default host ports while the smoke exports non-default ports.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    first_run = (REPO_ROOT / "scripts" / "first-run-smoke.sh").read_text()
    setup = (REPO_ROOT / "setup.sh").read_text()

    assert "127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432" in compose
    assert "127.0.0.1:${LITELLM_HOST_PORT:-4000}:4000" in compose
    assert "127.0.0.1:${QDRANT_HOST_PORT:-6333}:6333" in compose

    assert ': "${POSTGRES_HOST_PORT:=15432}"' in first_run
    assert ': "${LITELLM_HOST_PORT:=14000}"' in first_run
    assert ': "${QDRANT_HOST_PORT:=16333}"' in first_run
    assert ': "${PAPER_INGESTION_HOST_PORT:=18010}"' in first_run
    assert ': "${LEARNING_ENGINE_HOST_PORT:=18011}"' in first_run
    assert ': "${OLLAMA_HOST_PORT:=11444}"' in first_run

    assert '"${POSTGRES_HOST_PORT:-5432}"' in setup
    assert '"${LITELLM_HOST_PORT:-4000}"' in setup
    assert 'DASHBOARD_URL="http://localhost:${DASHBOARD_HOST_PORT_RESOLVED}"' in setup


def _compute(nvidia: str, override: str) -> str:
    """Source the lib and echo compute_compose_file's output."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; compute_compose_file {nvidia} {override}'],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_compute_gpu_no_override() -> None:
    assert _compute("1", "0") == "docker-compose.yml:docker-compose.gpu.yml"


def test_compute_cpu_no_override() -> None:
    assert _compute("0", "0") == "docker-compose.yml"


def test_compute_gpu_with_override_appended_last() -> None:
    assert (
        _compute("1", "1")
        == "docker-compose.yml:docker-compose.gpu.yml:docker-compose.override.yml"
    )


def test_upsert_is_idempotent(tmp_path: Path) -> None:
    """A second upsert of the same key must leave exactly one COMPOSE_FILE line."""
    env = tmp_path / ".env"
    env.write_text("FOO=bar\nCOMPOSE_FILE=old\nBAZ=qux\n")
    new_value = "docker-compose.yml:docker-compose.gpu.yml"
    script = (
        f'source "{LIB}"\n'
        f"upsert_env_var COMPOSE_FILE {new_value}\n"
        f"upsert_env_var COMPOSE_FILE {new_value}\n"
    )
    subprocess.run(
        ["bash", "-c", script], cwd=str(tmp_path), capture_output=True, text=True, check=True
    )
    lines = env.read_text().splitlines()
    compose_lines = [ln for ln in lines if ln.startswith("COMPOSE_FILE=")]
    assert compose_lines == [f"COMPOSE_FILE={new_value}"]
    # other keys are preserved untouched
    assert "FOO=bar" in lines
    assert "BAZ=qux" in lines


def test_upsert_overwrites_stale_gpu_entry_on_downgrade(tmp_path: Path) -> None:
    """A GPU→CPU re-run must overwrite a stale gpu.yml entry, not append a dupe."""
    env = tmp_path / ".env"
    env.write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml\n")
    script = f'source "{LIB}"\nupsert_env_var COMPOSE_FILE docker-compose.yml\n'
    subprocess.run(
        ["bash", "-c", script], cwd=str(tmp_path), capture_output=True, text=True, check=True
    )
    assert env.read_text().splitlines() == ["COMPOSE_FILE=docker-compose.yml"]


def _ollama_models(smart: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; compute_ollama_models {smart}'],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_ollama_models_includes_non_default_smart() -> None:
    # A bigger-tier smart model (not in the default OLLAMA_MODELS) must be pulled.
    assert _ollama_models("qwen3:14b") == "qwen3:14b,qwen3:4b,qwen3-embedding:4b"


def test_ollama_models_default_smart() -> None:
    assert _ollama_models("qwen3:8b") == "qwen3:8b,qwen3:4b,qwen3-embedding:4b"


def test_ollama_models_dedupes_when_smart_is_fast_or_embed() -> None:
    assert _ollama_models("qwen3:4b") == "qwen3:4b,qwen3-embedding:4b"
    assert _ollama_models("qwen3-embedding:4b") == "qwen3:4b,qwen3-embedding:4b"


_BASH = shutil.which("bash") or "/bin/bash"


def _resolve_smi(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run resolve_nvidia_smi with a controlled PATH / JARVIS_WSL_NVIDIA_SMI.

    bash is invoked by absolute path so the controlled (often near-empty) PATH
    only governs the in-shell `command -v nvidia-smi` lookup, not finding bash.
    """
    return subprocess.run(
        [_BASH, "-c", f'source "{LIB}"; resolve_nvidia_smi'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_resolve_nvidia_smi_prefers_path(tmp_path: Path) -> None:
    """When nvidia-smi is on PATH, that path is returned."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    smi = bindir / "nvidia-smi"
    smi.write_text("#!/bin/sh\n")
    smi.chmod(0o755)
    proc = _resolve_smi({"PATH": str(bindir)})
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(smi)


def test_resolve_nvidia_smi_falls_back_to_wsl_path(tmp_path: Path) -> None:
    """When nvidia-smi is NOT on PATH, the WSL2 fallback location is used."""
    empty = tmp_path / "empty"
    empty.mkdir()
    wsl = tmp_path / "wsl-nvidia-smi"
    wsl.write_text("#!/bin/sh\n")
    wsl.chmod(0o755)
    proc = _resolve_smi({"PATH": str(empty), "JARVIS_WSL_NVIDIA_SMI": str(wsl)})
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(wsl)


def test_resolve_nvidia_smi_returns_nonzero_when_absent(tmp_path: Path) -> None:
    """No nvidia-smi on PATH and no WSL binary → non-zero exit, empty output."""
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = _resolve_smi(
        {"PATH": str(empty), "JARVIS_WSL_NVIDIA_SMI": str(tmp_path / "nonexistent")}
    )
    assert proc.returncode == 1
    assert proc.stdout.strip() == ""


def _prereq_plan(
    os_name: str, os_id: str, has_apt: str, has_brew: str, *missing: str
) -> subprocess.CompletedProcess[str]:
    """Return the setup_lib prerequisite install plan for a synthetic host."""
    args = " ".join([os_name, os_id, has_apt, has_brew, *missing])
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"; prereq_install_plan {args}'],
        capture_output=True,
        text=True,
        check=False,
    )


def test_prereq_plan_for_ubuntu_apt_host() -> None:
    result = _prereq_plan("Linux", "ubuntu", "1", "0", "docker", "docker-compose", "openssl")
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "sudo apt-get update",
        "sudo apt-get install -y docker.io docker-compose-plugin openssl",
    ]


def test_prereq_plan_for_macos_homebrew_host() -> None:
    result = _prereq_plan("Darwin", "unknown", "0", "1", "docker", "docker-compose", "openssl")
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "brew install --cask docker",
        "brew install openssl",
    ]


def test_prereq_plan_rejects_unsupported_host() -> None:
    result = _prereq_plan("Linux", "arch", "0", "0", "docker")
    assert result.returncode != 0
    assert result.stdout == ""


def test_prereq_manual_guidance_is_actionable() -> None:
    proc = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; prereq_manual_guidance docker docker-compose openssl'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Install Docker Engine or Docker Desktop" in proc.stdout
    assert "Docker Compose v2 plugin" in proc.stdout
    assert "Install openssl" in proc.stdout
    assert "re-run ./setup.sh --check" in proc.stdout
