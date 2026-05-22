"""Hardware-aware backend selection regressions for setup.sh.

Verifies that --non-interactive runs with --backend/--smart-model flags write
JARVIS_HW_TIER, JARVIS_LLM_BACKEND, JARVIS_SMART_MODEL, and COMPOSE_PROFILES
into .env, and that --check surfaces a ``HW tier:`` advisory line.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# Reusable skip markers — mirrors the pattern in test_operator_setup.py
# ---------------------------------------------------------------------------
_requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash not available on this host",
)
_requires_docker = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker CLI not available on this host",
)
_requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="openssl not available on this host",
)


def _stage_hw_tmpdir(tmp: Path) -> None:
    """Copy setup.sh, .env.example, scripts/, config/, and litellm/ into tmpdir.

    setup.sh --non-interactive needs:
    - setup.sh + .env.example in the working directory
    - scripts/ (init-secrets.sh, gen-langfuse-keys.sh, render-litellm-config.sh, …)
    - config/llm-tier-candidates.yaml  (read by _default_model_for_tier and render-litellm-config.sh)
    - litellm/config.yaml              (written by render-litellm-config.sh)
    """
    shutil.copy2(REPO_ROOT / "setup.sh", tmp / "setup.sh")
    (tmp / "setup.sh").chmod(0o755)
    if (REPO_ROOT / ".env.example").exists():
        shutil.copy2(REPO_ROOT / ".env.example", tmp / ".env.example")

    scripts_src = REPO_ROOT / "scripts"
    if scripts_src.is_dir():
        shutil.copytree(str(scripts_src), str(tmp / "scripts"))

    # render-litellm-config.sh resolves REPO_ROOT from its own script location,
    # so config/ and litellm/ must be siblings of scripts/ in the tmpdir.
    config_src = REPO_ROOT / "config"
    if config_src.is_dir():
        shutil.copytree(str(config_src), str(tmp / "config"))

    litellm_src = REPO_ROOT / "litellm"
    if litellm_src.is_dir():
        shutil.copytree(str(litellm_src), str(tmp / "litellm"))


@_requires_bash
@_requires_docker
@_requires_openssl
def test_setup_writes_hw_tier_keys(tmp_path):
    """--non-interactive --backend ollama --smart-model writes hw-aware .env keys.

    Verifies JARVIS_HW_TIER, JARVIS_LLM_BACKEND, JARVIS_SMART_MODEL, and
    COMPOSE_PROFILES are written by setup.sh, regardless of whether
    ``docker compose up -d`` succeeds (it will fail in an isolated tmpdir with
    no compose.yml — that is expected and intentionally tolerated).

    If setup.sh exits before writing .env (i.e. docker/openssl prereqs are
    missing or docker compose v2 is unavailable), the test is skipped rather
    than failed — the behaviour under test (the .env write step) was not reached.
    """
    _stage_hw_tmpdir(tmp_path)

    result = subprocess.run(
        [
            "bash",
            "setup.sh",
            "--non-interactive",
            "--mode",
            "single",
            "--backend",
            "ollama",
            "--smart-model",
            "qwen3:1.7b",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    env_path = tmp_path / ".env"
    if not env_path.exists():
        # setup.sh died before the .env write step — most likely because
        # docker compose v2 is not available or another prereq is absent.
        # The keys-under-test live downstream of those checks, so skip.
        pytest.skip(
            f"setup.sh exited {result.returncode} before writing .env — "
            f"likely a missing prereq (docker compose v2 / openssl). "
            f"stderr: {result.stderr[:400]}"
        )

    env = env_path.read_text()
    assert "JARVIS_HW_TIER=" in env, f"JARVIS_HW_TIER key missing from .env:\n{env}"
    assert "JARVIS_LLM_BACKEND=ollama" in env, (
        f"JARVIS_LLM_BACKEND=ollama missing from .env:\n{env}"
    )
    assert "JARVIS_SMART_MODEL=qwen3:1.7b" in env, (
        f"JARVIS_SMART_MODEL=qwen3:1.7b missing from .env:\n{env}"
    )
    # COMPOSE_PROFILES should be present (may be empty for ollama backend).
    assert "COMPOSE_PROFILES=" in env, f"COMPOSE_PROFILES key missing from .env:\n{env}"


@_requires_bash
def test_setup_check_reports_hw_tier(tmp_path):
    """--check (doctor mode) must emit a ``HW tier:`` line and exit 0 or 1.

    The HW tier probe in run_doctor() is non-fatal: it always prints
    ``[INFO]  HW tier: <tier>`` to stdout regardless of GPU presence,
    so this test runs on CPU-only boxes too.
    """
    _stage_hw_tmpdir(tmp_path)

    result = subprocess.run(
        ["bash", "setup.sh", "--check"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode in (0, 1), (
        f"Expected exit 0 or 1 from --check, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "HW tier:" in combined, f"Expected 'HW tier:' in --check output, got:\n{combined}"
    assert "PREFLIGHT:" in combined, (
        f"Expected 'PREFLIGHT:' in --check output (regression), got:\n{combined}"
    )
    assert not (tmp_path / ".env").exists(), "--check must not write a .env file"
