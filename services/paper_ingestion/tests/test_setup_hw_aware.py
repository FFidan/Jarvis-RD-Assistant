"""Hardware-aware backend selection regressions for setup.sh.

Verifies that --non-interactive runs with --backend/--smart-model flags write
JARVIS_HW_TIER, JARVIS_LLM_BACKEND, JARVIS_SMART_MODEL, and COMPOSE_PROFILES
into .env, that --check surfaces a ``HW tier:`` advisory line, that
_default_model_for_tier survives a host python3 without PyYAML, and that a
re-run which keeps the existing .env still starts the stack.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


def _write_pyyaml_import_error_shim(tmp: Path) -> Path:
    """Return a PYTHONPATH dir whose ``yaml`` package raises ImportError."""
    pkg = tmp / "pyyaml_shim" / "yaml"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise ImportError('PyYAML unavailable (test shim)')\n")
    return pkg.parent


def _write_python3_wrapper(tmp: Path) -> Path:
    """Return a PATH dir whose ``python3`` execs the running interpreter.

    The running interpreter is the project venv, which has PyYAML — so the
    yaml-reading path is exercised deterministically regardless of what the
    host ``python3`` ships.
    """
    bin_dir = tmp / "pybin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    return bin_dir


def _write_docker_shim(tmp: Path, *, up_exit_code: int) -> tuple[Path, Path]:
    """Return (PATH dir with a ``docker`` stub, invocation log path).

    The stub logs every invocation, reports a v2 compose version, succeeds for
    ``docker info`` (the fatal daemon probe), and exits ``up_exit_code`` for
    any ``compose ... up -d ...`` call.
    """
    bin_dir = tmp / "dockerbin"
    bin_dir.mkdir()
    log = tmp / "docker-invocations.log"
    log.touch()
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        'case "$*" in\n'
        '  "compose version --short") echo "2.99.0" ;;\n'
        f'  compose*"up -d"*) exit {up_exit_code} ;;\n'
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bin_dir, log


def _run_default_model_for_tier(
    tmp: Path, tier: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source scripts/setup_lib.sh && _default_model_for_tier {tier} ollama"],
        cwd=str(tmp),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@_requires_bash
@_requires_openssl
def test_setup_writes_hw_tier_keys(tmp_path):
    """--non-interactive --backend ollama --smart-model writes hw-aware .env keys.

    Verifies JARVIS_HW_TIER, JARVIS_LLM_BACKEND, JARVIS_SMART_MODEL, and
    COMPOSE_PROFILES are written by setup.sh. docker is PATH-shimmed: ``docker
    info`` succeeds (the pre-prompt fatal daemon probe must pass, even on
    daemon-less hosts) and ``compose ... up -d`` fails so the run stops right
    after the .env write instead of waiting on services.

    If setup.sh exits before writing .env (i.e. openssl or another prereq is
    absent), the test is skipped rather than failed — the behaviour under test
    (the .env write step) was not reached.
    """
    _stage_hw_tmpdir(tmp_path)
    docker_bin, _log = _write_docker_shim(tmp_path, up_exit_code=1)
    env = dict(os.environ)
    env["PATH"] = f"{docker_bin}{os.pathsep}{env['PATH']}"

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
        env=env,
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


@_requires_bash
def test_default_model_fallback_without_pyyaml(tmp_path):
    """A python3 without PyYAML must yield the fallback model — and ONLY that.

    stdout of _default_model_for_tier is command-substituted into
    NI_SMART_MODEL (and from there into .env), so any stdout pollution
    corrupts the written config. The exact-match assertion guards that.
    """
    _stage_hw_tmpdir(tmp_path)
    python_bin = _write_python3_wrapper(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{python_bin}{os.pathsep}{env['PATH']}"
    env["PYTHONPATH"] = str(_write_pyyaml_import_error_shim(tmp_path))

    result = _run_default_model_for_tier(tmp_path, "ge-48", env)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout == "qwen3:30b-a3b\n", f"stdout polluted or wrong: {result.stdout!r}"
    assert "PyYAML" in result.stderr, f"expected stderr diagnostic, got: {result.stderr!r}"


@_requires_bash
def test_default_model_yaml_and_fallback_agree_per_tier(tmp_path):
    """The built-in fallback dict must mirror the YAML's per-tier answers.

    Runs _default_model_for_tier per tier twice — once with PyYAML available
    (the venv interpreter) and once with the ImportError shim — and asserts
    both paths print the same model.
    """
    _stage_hw_tmpdir(tmp_path)
    python_bin = _write_python3_wrapper(tmp_path)
    yaml_env = dict(os.environ)
    yaml_env["PATH"] = f"{python_bin}{os.pathsep}{yaml_env['PATH']}"
    yaml_env.pop("PYTHONPATH", None)
    no_yaml_env = dict(yaml_env, PYTHONPATH=str(_write_pyyaml_import_error_shim(tmp_path)))

    for tier in ("cpu", "lt-8", "8-16", "16-24", "24-48", "ge-48"):
        with_yaml = _run_default_model_for_tier(tmp_path, tier, yaml_env)
        without_yaml = _run_default_model_for_tier(tmp_path, tier, no_yaml_env)
        assert with_yaml.returncode == 0, f"{tier}: {with_yaml.stderr}"
        assert without_yaml.returncode == 0, f"{tier}: {without_yaml.stderr}"
        assert with_yaml.stdout == without_yaml.stdout != "", (
            f"{tier}: yaml path {with_yaml.stdout!r} != fallback {without_yaml.stdout!r}"
        )


@_requires_bash
@_requires_openssl
def test_setup_rerun_keep_env_empty_profile_args_starts_stack(tmp_path):
    """Keep-and-start path with empty KEEP_PROFILE_ARGS must not crash under set -u.

    A legacy .env with neither COMPOSE_PROFILES nor TELEGRAM_BOT_TOKEN leaves
    KEEP_PROFILE_ARGS=() empty.  On bash < 4.4, ``"${KEEP_PROFILE_ARGS[@]}"``
    under ``set -u`` raises "unbound variable" — guard: the portable idiom
    ``${ARR[@]+"${ARR[@]}"}`` must be used at the site.  Asserts that the script
    reaches the stack-start path with no ``--profile`` flags injected, pulls the
    published images first, and brings the stack up with ``--no-build``.
    """
    _stage_hw_tmpdir(tmp_path)
    # Minimal .env: no TELEGRAM_BOT_TOKEN, no COMPOSE_PROFILES → KEEP_PROFILE_ARGS stays empty.
    env_before = "JARVIS_SECRET_KEY=existing_secret\n"
    (tmp_path / ".env").write_text(env_before)
    docker_bin, log = _write_docker_shim(tmp_path, up_exit_code=0)
    env = dict(os.environ)
    env["PATH"] = f"{docker_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["bash", "setup.sh"],
        cwd=str(tmp_path),
        env=env,
        input="\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, (
        f"setup.sh crashed (KEEP_PROFILE_ARGS empty under set -u?)\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Keeping existing .env" in result.stdout
    invocations = log.read_text().splitlines()
    # The published images must be materialised BEFORE the stack starts, and the
    # bring-up must forbid a build: every published service pairs `pull_policy: missing`
    # with a `build:` block, so an image left missing here would be silently BUILT —
    # the multi-GB torch build this release exists to eliminate.
    assert any(i.startswith("compose pull ") for i in invocations), (
        f"published images not pulled before start; docker invocations: {invocations}"
    )
    assert "compose up -d --no-build" in invocations, (
        f"bring-up is missing the --no-build guard; docker invocations: {invocations}"
    )
    # The user's own configuration survives; only the derived torch-variant pair is
    # backfilled (a pre-1.1 .env has none, and without it a CUDA host would silently
    # resolve the CPU image tag).
    env_after = (tmp_path / ".env").read_text()
    assert env_before.strip() in env_after, "existing .env configuration must be preserved"
    assert "TORCH_VARIANT=cpu" in env_after, "the torch variant must be backfilled"


@_requires_bash
@_requires_openssl
def test_setup_rerun_keep_env_starts_stack(tmp_path):
    """Declining the overwrite prompt keeps .env AND starts the stack.

    The pre-v0.8 dead end ("Keeping existing .env. Exiting." with services
    down) must not return. A legacy .env without COMPOSE_PROFILES but with a
    TELEGRAM_BOT_TOKEN must derive ``--profile telegram`` — and must pull the
    telegram image too, since an image left missing would be silently BUILT.
    docker is PATH-shimmed; the invocation log proves the path taken.
    """
    _stage_hw_tmpdir(tmp_path)
    env_before = "TELEGRAM_BOT_TOKEN=123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    (tmp_path / ".env").write_text(env_before)
    docker_bin, log = _write_docker_shim(tmp_path, up_exit_code=0)
    env = dict(os.environ)
    env["PATH"] = f"{docker_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["bash", "setup.sh"],
        cwd=str(tmp_path),
        env=env,
        input="\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "Keeping existing .env" in result.stdout
    invocations = log.read_text().splitlines()
    pulls = [i for i in invocations if i.startswith("compose --profile telegram pull ")]
    assert pulls, f"published images not pulled before start; docker invocations: {invocations}"
    assert "telegram_bot" in pulls[0], (
        "the telegram profile is active, so its image must be pulled too — otherwise "
        f"`up` would silently BUILD it; got: {pulls[0]}"
    )
    assert "compose --profile telegram up -d --no-build" in invocations, (
        f"bring-up is missing the --no-build guard; docker invocations: {invocations}"
    )
    env_after = (tmp_path / ".env").read_text()
    assert env_before.strip() in env_after, "existing .env configuration must be preserved"
    assert "TORCH_VARIANT=cpu" in env_after, "the torch variant must be backfilled"


def _write_missing_compose_docker_shim(tmp: Path) -> Path:
    """Return a PATH dir whose docker exists but lacks compose v2."""
    bin_dir = tmp / "missing-compose-bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  "compose version"*) exit 1 ;;\n'
        '  "info"*) exit 1 ;;\n'
        "esac\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    return bin_dir


def _write_sudo_logger(tmp: Path) -> tuple[Path, Path]:
    """Return a PATH dir with sudo that logs reviewed installer commands."""
    bin_dir = tmp / "sudo-bin"
    bin_dir.mkdir()
    log = tmp / "sudo-invocations.log"
    log.touch()
    sudo = bin_dir / "sudo"
    sudo.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n")
    sudo.chmod(0o755)
    return bin_dir, log


def _write_curl_logger(tmp: Path) -> tuple[Path, Path]:
    """Return a PATH dir with curl that logs fetches and creates the -o target.

    The plan's unprivileged fetch lines run for real during the guided install,
    so a curl shim keeps the test offline; it creates the ``-o`` target so the
    plan's later steps (sed -i on the fetched file) still succeed.
    """
    bin_dir = tmp / "curl-bin"
    bin_dir.mkdir()
    log = tmp / "curl-invocations.log"
    log.touch()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        'prev=""; out=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        '[ -n "$out" ] && : > "$out"\n'
        "exit 0\n"
    )
    curl.chmod(0o755)
    return bin_dir, log


@_requires_bash
def test_setup_noninteractive_missing_prereqs_requires_explicit_install_flag(tmp_path):
    """Noninteractive setup must not install host packages without opt-in."""
    _stage_hw_tmpdir(tmp_path)
    docker_bin = _write_missing_compose_docker_shim(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{docker_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["bash", "setup.sh", "--non-interactive", "--mode", "single"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Guided prerequisite installer would run" in combined
    assert "sudo apt-get" in combined
    assert "--install-prereqs" in combined
    assert not (tmp_path / ".env").exists()


@_requires_bash
def test_setup_check_install_prereqs_stays_read_only(tmp_path):
    """--check never executes installer commands, even with --install-prereqs."""
    _stage_hw_tmpdir(tmp_path)
    docker_bin = _write_missing_compose_docker_shim(tmp_path)
    sudo_bin, sudo_log = _write_sudo_logger(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{sudo_bin}{os.pathsep}{docker_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["bash", "setup.sh", "--check", "--install-prereqs"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode in (0, 1)
    assert "PREFLIGHT:" in combined
    assert sudo_log.read_text() == ""
    assert not (tmp_path / ".env").exists()


@_requires_bash
def test_setup_install_prereqs_runs_reviewed_plan_only_when_flagged(tmp_path):
    """--install-prereqs executes exactly the reviewed plan, printed first.

    The plan installs Docker from Docker's official apt repository (the stock
    distro packages miss the compose plugin), so its exact line count varies by
    host (a GPU host appends the NVIDIA Container Toolkit steps).  Instead of a
    literal list, this asserts the root-escalation contract from setup_lib.sh:
    the plan is echoed for review BEFORE anything runs, every privileged command
    executed is a plan line rewritten to ``sudo -n`` (non-interactive runs must
    never hang on a password prompt) in plan order with nothing extra, every
    fetch is a plan line downloading to a file — never piped to a shell — and
    the repo is pinned to the fetched signing key.  stderr is merged into stdout
    so the print-before-run ordering is observable.
    """
    _stage_hw_tmpdir(tmp_path)
    docker_bin = _write_missing_compose_docker_shim(tmp_path)
    sudo_bin, sudo_log = _write_sudo_logger(tmp_path)
    curl_bin, curl_log = _write_curl_logger(tmp_path)
    env = dict(os.environ)
    env["PATH"] = (
        f"{curl_bin}{os.pathsep}{sudo_bin}{os.pathsep}{docker_bin}{os.pathsep}{env['PATH']}"
    )

    result = subprocess.run(
        ["bash", "setup.sh", "--non-interactive", "--install-prereqs", "--mode", "single"],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        check=False,
    )

    lines = result.stdout.splitlines()
    assert "Guided prerequisite installer would run:" in lines, result.stdout
    marker = lines.index("Guided prerequisite installer would run:")
    # Plan lines follow the marker until the next [INFO]/[ERROR]-prefixed line.
    plan: list[str] = []
    for line in lines[marker + 1 :]:
        if line.startswith("["):
            break
        plan.append(line)
    run_indices = [i for i, line in enumerate(lines) if line.startswith("[INFO]  Running: ")]
    assert run_indices and marker < run_indices[0], (
        f"plan must be printed for review before anything runs:\n{result.stdout}"
    )

    assert "sudo apt-get update" in plan
    assert any(
        line.startswith("sudo apt-get install") and "docker-compose-plugin" in line for line in plan
    ), f"plan must install the compose plugin: {plan}"
    keyring = "/etc/apt/keyrings/docker.gpg"
    assert any(line.startswith("sudo gpg --dearmor") and keyring in line for line in plan), (
        f"signing key must land in a keyring file: {plan}"
    )
    assert any(f"signed-by={keyring}" in line and "download.docker.com" in line for line in plan), (
        f"apt repo must be pinned to the fetched key: {plan}"
    )
    for line in plan:
        assert not ("curl" in line and "|" in line), f"piped curl-to-shell in plan: {line}"

    expected_sudo = [
        f"-n {line.removeprefix('sudo ')}" for line in plan if line.startswith("sudo ")
    ]
    assert expected_sudo, f"plan has no privileged steps: {plan}"
    assert sudo_log.read_text().splitlines() == expected_sudo, (
        "executed privileged commands must be exactly the reviewed plan's sudo "
        f"lines under sudo -n, in order; plan: {plan}"
    )
    expected_curl = [line.removeprefix("curl ") for line in plan if line.startswith("curl ")]
    assert curl_log.read_text().splitlines() == expected_curl, (
        f"executed fetches must be exactly the reviewed plan's curl lines; plan: {plan}"
    )
    assert result.returncode != 0  # docker shim still lacks compose after the fake install.
    assert not (tmp_path / ".env").exists()
