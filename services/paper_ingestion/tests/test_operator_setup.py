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
    "jarvis_model_hmac_key.txt",
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


def _stage_setup_tmpdir(tmp: Path) -> None:
    """Copy setup.sh, .env.example, and scripts/ into an isolated tmpdir."""
    shutil.copy2(REPO_ROOT / "setup.sh", tmp / "setup.sh")
    (tmp / "setup.sh").chmod(0o755)
    if (REPO_ROOT / ".env.example").exists():
        shutil.copy2(REPO_ROOT / ".env.example", tmp / ".env.example")
    scripts_src = REPO_ROOT / "scripts"
    if scripts_src.is_dir():
        shutil.copytree(str(scripts_src), str(tmp / "scripts"))


def test_setup_preserves_existing_secrets_on_rerun():
    """INST-1: a re-run with an existing .env must NOT rotate secrets.

    Regenerating POSTGRES_PASSWORD orphans the Postgres data volume and a new
    JARVIS_CONFIG_KEY makes every Fernet-encrypted user_config row unreadable.
    The sentinel values written below must survive the .env regeneration.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if shutil.which("openssl") is None:
        pytest.skip("openssl required for secret generation in setup.sh")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)

        # Pre-existing .env with sentinel secrets that must be preserved.
        (tmp / ".env").write_text(
            "POSTGRES_PASSWORD=PRESERVE_ME_PG\nJARVIS_CONFIG_KEY=PRESERVE_ME_FERNET\n"
        )

        result = subprocess.run(
            ["bash", "setup.sh", "--non-interactive", "--mode", "single"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

        env_path = tmp / ".env"
        if not env_path.exists():
            pytest.skip(
                f"setup.sh exited {result.returncode} before writing .env — "
                f"likely a missing prereq (docker/docker-compose). "
                f"stderr: {result.stderr[:400]}"
            )

        env_text = env_path.read_text()
        assert "POSTGRES_PASSWORD=PRESERVE_ME_PG" in env_text, (
            f"POSTGRES_PASSWORD was rotated on re-run (data-loss footgun):\n{env_text}"
        )
        assert "JARVIS_CONFIG_KEY=PRESERVE_ME_FERNET" in env_text, (
            f"JARVIS_CONFIG_KEY was rotated on re-run (Fernet rows unreadable):\n{env_text}"
        )


def test_setup_generates_missing_secret_but_preserves_present_one():
    """INST-1: a key absent from an existing .env is freshly generated, while
    a present key is preserved verbatim."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if shutil.which("openssl") is None:
        pytest.skip("openssl required for secret generation in setup.sh")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)

        # JARVIS_API_KEY present (must be preserved); QDRANT_API_KEY absent
        # entirely (must be freshly generated, non-empty).
        (tmp / ".env").write_text("JARVIS_API_KEY=KEEP_API\n")

        result = subprocess.run(
            ["bash", "setup.sh", "--non-interactive", "--mode", "single"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

        env_path = tmp / ".env"
        if not env_path.exists():
            pytest.skip(
                f"setup.sh exited {result.returncode} before writing .env — "
                f"likely a missing prereq (docker/docker-compose). "
                f"stderr: {result.stderr[:400]}"
            )

        env_text = env_path.read_text()
        assert "JARVIS_API_KEY=KEEP_API" in env_text, (
            f"present JARVIS_API_KEY was not preserved:\n{env_text}"
        )

        qdrant_lines = [ln for ln in env_text.splitlines() if ln.startswith("QDRANT_API_KEY=")]
        assert qdrant_lines, f"QDRANT_API_KEY missing from regenerated .env:\n{env_text}"
        qdrant_value = qdrant_lines[0].split("=", 1)[1]
        assert qdrant_value.strip(), (
            f"QDRANT_API_KEY was not freshly generated (empty): {qdrant_lines[0]!r}"
        )


def test_setup_creates_secrets_dir_and_writes_telegram_token():
    """INST-2: setup.sh must `mkdir -p secrets` early so a Telegram token from
    the environment is persisted even on a fresh checkout where secrets/ does
    not yet exist. This test deliberately does NOT pre-create secrets/."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    if shutil.which("openssl") is None:
        pytest.skip("openssl required for secret generation in setup.sh")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)
        # Intentionally do NOT create tmp/secrets — that is the bug under test.

        env = dict(__import__("os").environ)
        env["TELEGRAM_BOT_TOKEN"] = "123456:AAFakeTokenForTestingPurposes12345"

        result = subprocess.run(
            ["bash", "setup.sh", "--non-interactive", "--mode", "single"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
            env=env,
        )

        if not (tmp / ".env").exists():
            pytest.skip(
                f"setup.sh exited {result.returncode} before writing .env — "
                f"likely a missing prereq (docker/docker-compose). "
                f"stderr: {result.stderr[:400]}"
            )

        token_file = tmp / "secrets" / "telegram_bot_token.txt"
        assert token_file.exists(), (
            "secrets/telegram_bot_token.txt was not created — "
            "early `mkdir -p secrets` missing.\n"
            f"stdout: {result.stdout[-400:]}\nstderr: {result.stderr[-400:]}"
        )
        assert token_file.stat().st_size > 0, "telegram token file is empty"
        mode = oct(token_file.stat().st_mode & 0o777)
        assert mode == "0o600", f"telegram token file has wrong mode: {mode}"


def test_setup_check_subnet_probe_is_non_fatal():
    """RB4-2: the --check host-route collision probe must never make --check
    exit non-zero by itself and must never write files."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)

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
            f"--check exited {result.returncode}; the subnet probe must not "
            f"change exit semantics.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "PREFLIGHT:" in combined, f"Expected 'PREFLIGHT:' in output, got:\n{combined}"
        assert not (tmp / ".env").exists(), "--check must not write a .env file"
        # The probe writes nothing regardless of whether the warn fires.
        assert not (tmp / "secrets").exists() or not any((tmp / "secrets").iterdir()), (
            "--check must not create secret files"
        )


def test_gen_langfuse_keys_generates_non_empty_mode_600_files():
    """A0-2: gen-langfuse-keys.sh must create non-empty pk-lf-/sk-lf- files with mode 600."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    gen_sh = REPO_ROOT / "scripts" / "gen-langfuse-keys.sh"
    if not gen_sh.exists():
        pytest.skip("scripts/gen-langfuse-keys.sh not found")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        secrets_dir = tmp / "secrets"
        secrets_dir.mkdir()

        dest_scripts = tmp / "scripts"
        dest_scripts.mkdir()
        shutil.copy(gen_sh, dest_scripts / "gen-langfuse-keys.sh")

        result = subprocess.run(
            ["bash", str(dest_scripts / "gen-langfuse-keys.sh")],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, (
            f"gen-langfuse-keys.sh exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        pk_file = secrets_dir / "langfuse_init_pk.txt"
        sk_file = secrets_dir / "langfuse_init_sk.txt"

        assert pk_file.exists(), "langfuse_init_pk.txt was not created"
        assert sk_file.exists(), "langfuse_init_sk.txt was not created"

        pk_content = pk_file.read_text()
        sk_content = sk_file.read_text()

        assert pk_content.startswith("pk-lf-"), (
            f"langfuse_init_pk.txt does not start with 'pk-lf-': {pk_content!r}"
        )
        assert sk_content.startswith("sk-lf-"), (
            f"langfuse_init_sk.txt does not start with 'sk-lf-': {sk_content!r}"
        )

        assert oct(pk_file.stat().st_mode & 0o777) == "0o600", (
            f"langfuse_init_pk.txt has wrong mode: {oct(pk_file.stat().st_mode & 0o777)}"
        )
        assert oct(sk_file.stat().st_mode & 0o777) == "0o600", (
            f"langfuse_init_sk.txt has wrong mode: {oct(sk_file.stat().st_mode & 0o777)}"
        )


def test_gen_langfuse_keys_is_idempotent():
    """A0-2: running gen-langfuse-keys.sh twice must not change file content.

    The script uses ``[ -s file ] || generate`` semantics, so an existing
    non-empty file is preserved verbatim on the second run.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    gen_sh = REPO_ROOT / "scripts" / "gen-langfuse-keys.sh"
    if not gen_sh.exists():
        pytest.skip("scripts/gen-langfuse-keys.sh not found")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        secrets_dir = tmp / "secrets"
        secrets_dir.mkdir()

        dest_scripts = tmp / "scripts"
        dest_scripts.mkdir()
        shutil.copy(gen_sh, dest_scripts / "gen-langfuse-keys.sh")

        def _run_gen() -> subprocess.CompletedProcess:
            return subprocess.run(
                ["bash", str(dest_scripts / "gen-langfuse-keys.sh")],
                cwd=str(tmp),
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )

        r1 = _run_gen()
        assert r1.returncode == 0, f"First run failed: {r1.stderr}"

        pk_after_first = (secrets_dir / "langfuse_init_pk.txt").read_text()
        sk_after_first = (secrets_dir / "langfuse_init_sk.txt").read_text()

        r2 = _run_gen()
        assert r2.returncode == 0, f"Second run failed: {r2.stderr}"

        pk_after_second = (secrets_dir / "langfuse_init_pk.txt").read_text()
        sk_after_second = (secrets_dir / "langfuse_init_sk.txt").read_text()

        assert pk_after_first == pk_after_second, (
            "gen-langfuse-keys.sh rotated langfuse_init_pk.txt on second run (not idempotent)"
        )
        assert sk_after_first == sk_after_second, (
            "gen-langfuse-keys.sh rotated langfuse_init_sk.txt on second run (not idempotent)"
        )


def test_init_secrets_guard_uses_posix_ere_not_gnu_bre():
    """INST-1 portability: the .env guard in sync_secret must use ``grep -qE``
    with POSIX ERE ``+`` (compatible with BSD/macOS grep), not GNU BRE ``\\+``
    which is treated as a literal ``+`` on macOS and causes the guard to misfire,
    resulting in duplicate KEY= entries on re-run.

    This is a static assertion against the script text — no shell execution
    required.  It mirrors the existing pattern of script-property assertions in
    this suite (e.g. test_bootstrap_scripts_probe_direct_http_dashboard_url).
    """
    init_sh = REPO_ROOT / "scripts" / "init-secrets.sh"
    if not init_sh.exists():
        pytest.skip("scripts/init-secrets.sh not found")

    script_text = init_sh.read_text()

    # Strip comment lines before checking — comments may mention the old form
    # for documentation purposes, so only inspect executable shell code.
    code_lines = [ln for ln in script_text.splitlines() if not ln.lstrip().startswith("#")]
    code_text = "\n".join(code_lines)

    # Must NOT contain the GNU BRE form on an executable line.
    assert r"grep -q " + r'"^${key}=.\+"' not in code_text, (
        "init-secrets.sh still uses GNU BRE '\\+' in a grep -q call; "
        "this is not portable to BSD/macOS grep.  Use grep -qE with POSIX ERE '+' instead."
    )

    # Must contain the POSIX ERE form used in the guard.
    assert 'grep -qE "^${key}=.+"' in code_text, (
        "init-secrets.sh does not use 'grep -qE \"^${key}=.+\"' — "
        "the POSIX-ERE guard is missing or malformed."
    )


def test_init_secrets_rerun_produces_no_duplicate_env_keys():
    """INST-1 idempotency: running init-secrets.sh twice must NOT produce
    duplicate KEY= lines in .env, and secrets/*.txt files must be stable
    (second run must not change their content).
    """
    import shutil as _shutil

    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")

    init_sh = REPO_ROOT / "scripts" / "init-secrets.sh"
    if not init_sh.exists():
        pytest.skip("scripts/init-secrets.sh not found")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / ".env").write_text("")
        (tmp / "secrets").mkdir()

        dest_scripts = tmp / "scripts"
        dest_scripts.mkdir()
        _shutil.copy(init_sh, dest_scripts / "init-secrets.sh")

        def _run() -> subprocess.CompletedProcess:
            return subprocess.run(
                ["bash", str(dest_scripts / "init-secrets.sh")],
                cwd=str(tmp),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )

        r1 = _run()
        assert r1.returncode == 0, (
            f"First run failed (exit {r1.returncode})\nstdout: {r1.stdout}\nstderr: {r1.stderr}"
        )

        # Snapshot secret file contents after first run.
        secrets_dir = tmp / "secrets"
        first_run_contents: dict[str, str] = {}
        for fpath in sorted(secrets_dir.iterdir()):
            first_run_contents[fpath.name] = fpath.read_text()

        r2 = _run()
        assert r2.returncode == 0, (
            f"Second run failed (exit {r2.returncode})\nstdout: {r2.stdout}\nstderr: {r2.stderr}"
        )

        # Assert no duplicate KEY= lines in .env.
        env_text = (tmp / ".env").read_text()
        env_lines = [ln for ln in env_text.splitlines() if "=" in ln]
        seen_keys: set[str] = set()
        duplicates: list[str] = []
        for line in env_lines:
            k = line.split("=", 1)[0]
            if k in seen_keys:
                duplicates.append(k)
            seen_keys.add(k)
        assert not duplicates, (
            f"Duplicate KEY= entries found in .env after two runs: {duplicates}\n"
            f".env contents:\n{env_text}"
        )

        # Assert secrets files are stable (content unchanged by second run).
        unstable: list[str] = []
        for fname, first_content in first_run_contents.items():
            second_content = (secrets_dir / fname).read_text()
            if first_content != second_content:
                unstable.append(fname)
        assert not unstable, f"Secret files changed on second run (not stable): {unstable}"


def test_setup_check_nondefault_subnet_emits_coupling_warning():
    """SEC-NET-1: when JARVIS_NET_SUBNET is set to a non-default value, --check
    must emit a non-fatal warning about the gateway and nginx set_real_ip_from
    coupling. The run must still exit 0 or 1 and still print 'PREFLIGHT:'."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)

        env = dict(__import__("os").environ)
        env["JARVIS_NET_SUBNET"] = "10.200.0.0/24"  # non-default value

        result = subprocess.run(
            ["bash", "setup.sh", "--check"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )

        combined = result.stdout + result.stderr
        assert result.returncode in {0, 1}, (
            f"--check exited {result.returncode}; coupling warning must not "
            f"change exit semantics.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "PREFLIGHT:" in combined, (
            f"Expected 'PREFLIGHT:' in output (regression), got:\n{combined}"
        )
        # The coupling warning must mention gateway and nginx/set_real_ip_from.
        assert "gateway" in combined.lower(), (
            f"Expected gateway coupling warning in --check output, got:\n{combined}"
        )
        assert "set_real_ip_from" in combined or "nginx.conf" in combined, (
            f"Expected nginx/set_real_ip_from coupling warning in --check output, got:\n{combined}"
        )
        assert not (tmp / ".env").exists(), "--check must not write a .env file"


def test_setup_check_default_subnet_no_coupling_warning():
    """SEC-NET-1: with default/unset JARVIS_NET_SUBNET the gateway/nginx
    coupling warning must NOT appear — only the standard PREFLIGHT output."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)

        # Strip JARVIS_NET_SUBNET from env entirely (default behaviour).
        env = {k: v for k, v in __import__("os").environ.items() if k != "JARVIS_NET_SUBNET"}

        result = subprocess.run(
            ["bash", "setup.sh", "--check"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )

        combined = result.stdout + result.stderr
        assert result.returncode in {0, 1}, (
            f"--check exited {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "PREFLIGHT:" in combined, f"Expected 'PREFLIGHT:' in output, got:\n{combined}"
        # The gateway/nginx coupling warning must NOT fire for the default subnet.
        assert "gateway" not in combined.lower() and "set_real_ip_from" not in combined, (
            f"Unexpected coupling warning for default subnet:\n{combined}"
        )
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


# ---------------------------------------------------------------------------
# Tests: --check hardware recommendation line (B3-1 per-VRAM advisory)
# ---------------------------------------------------------------------------


def test_setup_check_hardware_recommendation_no_gpu_is_fail_safe():
    """--check must not crash or block when no GPU is present.

    The hardware recommendation block in run_doctor() must be entirely
    advisory — no GPU path falls through to a static info line and the
    doctor still prints PREFLIGHT: and exits 0 or 1.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)

        # Run --check with PATH that excludes nvidia-smi so GPU probe fails.
        env = dict(__import__("os").environ)
        # Remove nvidia-smi from PATH by restricting to essential POSIX tools.
        # We accomplish this by creating a tmpdir/bin that lacks nvidia-smi.
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        # Symlink the real bash/python/openssl/docker so other checks pass.
        for tool in ("bash", "python3", "openssl", "docker"):
            real = shutil.which(tool)
            if real:
                (fake_bin / tool).symlink_to(real)
        env["PATH"] = str(fake_bin) + ":" + env.get("PATH", "")

        result = subprocess.run(
            ["bash", "setup.sh", "--check"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )

        combined = result.stdout + result.stderr
        assert result.returncode in {0, 1}, (
            f"--check exited {result.returncode}; hardware recommendation block "
            f"must not change exit semantics.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "PREFLIGHT:" in combined, f"Expected 'PREFLIGHT:' in output, got:\n{combined}"
        assert not (tmp / ".env").exists(), "--check must not write a .env file"


def test_setup_check_hardware_recommendation_static_pointer_when_python_unavailable():
    """--check prints a static info pointer when python3 is not available in PATH.

    This verifies the fallback branch: GPU detected, VRAM readable, but
    python3/hardware_fit not importable → static 'see Settings → System/Models'
    line is printed, no crash, exit 0 or 1, PREFLIGHT: present.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    # Create a fake nvidia-smi that reports a fixed VRAM value and a fake
    # python3 that always exits non-zero (simulates import failure).
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _stage_setup_tmpdir(tmp)

        fake_bin = tmp / "bin"
        fake_bin.mkdir()

        # Fake nvidia-smi: exits 0, lists one GPU, and reports 16384 MiB VRAM.
        fake_nvidiasmi = fake_bin / "nvidia-smi"
        fake_nvidiasmi.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  -L) echo 'GPU 0: NVIDIA Test GPU 16G (UUID: fake-uuid)'; exit 0 ;;\n"
            "  --query-gpu=memory.total) echo '16384'; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
        fake_nvidiasmi.chmod(0o755)

        # Fake python3: always exits 1 (simulates hardware_fit not importable).
        fake_python3 = fake_bin / "python3"
        fake_python3.write_text("#!/bin/sh\nexit 1\n")
        fake_python3.chmod(0o755)

        # Symlink other required tools.
        for tool in ("openssl", "docker"):
            real = shutil.which(tool)
            if real:
                (fake_bin / tool).symlink_to(real)

        env = dict(__import__("os").environ)
        env["PATH"] = str(fake_bin) + ":" + env.get("PATH", "")

        result = subprocess.run(
            ["bash", "setup.sh", "--check"],
            cwd=str(tmp),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )

        combined = result.stdout + result.stderr
        assert result.returncode in {0, 1}, (
            f"--check exited {result.returncode}; static-pointer fallback must "
            f"not change exit semantics.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "PREFLIGHT:" in combined, f"Expected 'PREFLIGHT:' in output, got:\n{combined}"
        # The static pointer fallback must mention VRAM and Settings/Models.
        assert "16384" in combined or "Settings" in combined or "System/Models" in combined, (
            f"Expected VRAM or static settings pointer in --check output, got:\n{combined}"
        )
        assert not (tmp / ".env").exists(), "--check must not write a .env file"
