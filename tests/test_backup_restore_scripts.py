"""Invariants for backup, restore, quarantine, and recovery scripts.

Authenticated manifests bind every archive digest before restore accepts it.
The same checks cover request admission and consumption, pre-swap authentication
invalidation, vector checkpoint rotation, recovery, outbound quarantine, and
required Compose mounts.

The scripts expose selected helpers through ``--functions-only`` so validation
uses their OpenSSL invocations and restore decisions directly.
"""

import base64
import hashlib
import http.client
import io
import json
import os
import pty
import socket
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
RESTORE_SH = REPO_ROOT / "scripts" / "restore.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"
LITELLM_ENTRYPOINT = REPO_ROOT / "scripts" / "litellm-entrypoint.sh"

BREAK_GLASS_PHRASE = "I-ACCEPT-UNVERIFIED-BACKUP"
TS = "20260719_120000"
# Written by the openssl shim `_gate` installs; its presence is the evidence that the
# gate actually recomputed a MAC rather than short-circuiting past the signature.
OPENSSL_TRACE = "openssl-calls"


@pytest.fixture(scope="module")
def backup_src() -> str:
    return BACKUP_SH.read_text()


@pytest.fixture(scope="module")
def restore_src() -> str:
    return RESTORE_SH.read_text()


def _run_bash(body: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a bash snippet from the repo root with stdin detached (no tty)."""
    return subprocess.run(
        ["bash", "-c", body],
        cwd=REPO_ROOT,
        env={**os.environ, **(env or {})},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


class _ReachableHandler(BaseHTTPRequestHandler):
    """Return an HTTP failure while keeping the TCP endpoint reachable."""

    def do_GET(self) -> None:
        self.send_response(503)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _reachable_http_server() -> Iterator[ThreadingHTTPServer]:
    """Run a temporary loopback endpoint that always returns HTTP 503.

    Yields
    ------
    ThreadingHTTPServer
        The running server and its dynamically allocated address.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReachableHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _tcp_accepts(port: int) -> bool:
    """Return whether a loopback TCP connection succeeds.

    Parameters
    ----------
    port
        Loopback port to probe.

    Returns
    -------
    bool
        ``True`` when the connection succeeds.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def _wait_for_tcp(port: int, *, accepting: bool, timeout: float = 3.0) -> bool:
    """Wait for a loopback endpoint to enter the requested state.

    Parameters
    ----------
    port
        Loopback port to probe.
    accepting
        Expected connection state.
    timeout
        Maximum wait in seconds.

    Returns
    -------
    bool
        ``True`` when the expected state is observed before the deadline.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_accepts(port) is accepting:
            return True
        time.sleep(0.05)
    return False


@contextmanager
def _litellm_entrypoint_process(env: dict[str, str]) -> Iterator[subprocess.Popen[str]]:
    """Run the LiteLLM entrypoint and guarantee child-process cleanup.

    Parameters
    ----------
    env
        Complete environment for the entrypoint process.

    Yields
    ------
    subprocess.Popen[str]
        The running wrapper process.
    """
    proc = subprocess.Popen(
        ["sh", str(LITELLM_ENTRYPOINT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


def _run_bash_on_tty(body: str, typed: str, *, env: dict[str, str] | None = None) -> str:
    """Run a bash snippet with stdin attached to a pty and `typed` sent to the prompt.

    restore.sh's break-glass path requires an interactive stdin, so driving it needs a
    real terminal rather than a pipe.
    """
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", "-c", body],
            cwd=REPO_ROOT,
            env={**os.environ, **(env or {})},
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        os.close(slave)
        slave = -1
        os.write(master, typed.encode())
        out, _ = proc.communicate(timeout=60)
        return out
    finally:
        if slave != -1:
            os.close(slave)
        os.close(master)


@pytest.fixture
def signed_set(tmp_path: Path) -> Path:
    """A backup key plus a manifest signed with backup.sh's own signing helper."""
    key = tmp_path / "backup_key"
    key.write_text("a-backup-encryption-key\n")
    manifest = tmp_path / f"manifest_{TS}.json"
    manifest.write_text(f'{{"timestamp":"{TS}","schema_version":104,"archives":[]}}')
    result = _run_bash(
        "source scripts/backup.sh --functions-only;"
        f' ENC_KEYFILE="{key}"; sign_manifest "{manifest}"'
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"manifest_{TS}.json.hmac").exists()
    return tmp_path


@pytest.fixture
def signed_legacy_set(tmp_path: Path) -> Path:
    """Create a v1.1.3-shape manifest signed by the production backup helper."""
    key = tmp_path / "backup_key"
    key.write_text("a-backup-encryption-key\n")
    archives = []
    for name, content in (
        (f"jarvis_{TS}.sql.gz", b"jarvis-v1.1.3"),
        (f"litellm_{TS}.sql.gz", b"litellm-v1.1.3"),
        (f"secrets_{TS}.tar.gz", b"secrets-v1.1.3"),
    ):
        (tmp_path / name).write_bytes(content)
        archives.append(
            {
                "filename": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    manifest = tmp_path / f"manifest_{TS}.json"
    manifest.write_text(
        json.dumps(
            {
                "timestamp": TS,
                "app_version": "1.1.3",
                "schema_version": 102,
                "created_at": "2026-07-19T12:00:00+00:00",
                "archives": archives,
            },
            separators=(",", ":"),
        )
    )
    _sign_manifest(tmp_path)
    return tmp_path


def _sign_manifest(archive_dir: Path) -> None:
    """Sign the fixture with backup.sh's implementation, replacing an old MAC."""
    signature = archive_dir / f"manifest_{TS}.json.hmac"
    signature.unlink(missing_ok=True)
    result = _run_bash(
        "source scripts/backup.sh --functions-only;"
        f' ENC_KEYFILE="{archive_dir / "backup_key"}";'
        f' sign_manifest "{archive_dir}/manifest_{TS}.json"'
    )
    assert result.returncode == 0, result.stderr


def _authenticate_and_verify_inventory(archive_dir: Path) -> str:
    """Run restore signature validation, parsing, and exact-inventory verification."""
    result = _run_bash(
        "set -euo pipefail\n"
        "source scripts/restore.sh --functions-only\n"
        f'ENC_KEYFILE="{archive_dir}/backup_key"\n'
        "SOURCE=inbox\n"
        f"TIMESTAMP={TS}\n"
        f'MANIFEST="{archive_dir}/manifest_{TS}.json"\n'
        f'MANIFEST_HMAC_MARKER="{archive_dir}/marker"\n'
        "gate_manifest_signature\n"
        '[ "$MANIFEST_AUTHENTICATED" = 1 ]\n'
        f'parse_authenticated_manifest "$MANIFEST" "$TIMESTAMP" "{archive_dir}/inventory.tsv"\n'
        f'verify_manifest_inventory "{archive_dir}" "$TIMESTAMP" "{archive_dir}/inventory.tsv"\n'
        "echo ACCEPTED\n"
    )
    return result.stdout + result.stderr


def _verify(archive_dir: Path, *, key_name: str = "backup_key") -> bool:
    result = _run_bash(
        f"source scripts/restore.sh --functions-only;"
        f' ENC_KEYFILE="{archive_dir / key_name}";'
        f' verify_manifest_signature "{archive_dir}/manifest_{TS}.json"'
    )
    return result.returncode == 0


def _gate(
    archive_dir: Path,
    *,
    source: str,
    marker: bool,
    env: dict[str, str] | None = None,
    typed: str | None = None,
) -> str:
    """Run restore.sh's pre-destruction manifest gate and return its combined output.

    Emits `PROCEED` only when the gate returned; a refusal terminates via
    fail_before_destruction, whose recorded operator-facing message is echoed as
    `ERROR=...` by the trap installed here. Every run shadows `openssl` with a
    recording wrapper so a test can assert that verification really ran.
    """
    marker_path = archive_dir / ("marker" if marker else "absent-marker")
    if marker:
        marker_path.touch()
    body = (
        "source scripts/restore.sh --functions-only\n"
        "trap 'echo \"ERROR=$ERROR\"' EXIT\n"
        'openssl() { printf "%s\\n" "$1" >> "' + str(archive_dir / OPENSSL_TRACE) + '";'
        ' command openssl "$@"; }\n'
        f'ENC_KEYFILE="{archive_dir}/backup_key"\n'
        f'SOURCE="{source}"\n'
        f"TIMESTAMP={TS}\n"
        f'MANIFEST="{archive_dir}/manifest_{TS}.json"\n'
        f'MANIFEST_HMAC_MARKER="{marker_path}"\n'
        "gate_manifest_signature\n"
        "echo PROCEED\n"
    )
    if typed is not None:
        return _run_bash_on_tty(body, typed, env=env)
    result = _run_bash(body, env=env)
    return result.stdout + result.stderr


# --- Script shape -----------------------------------------------------------------


def test_backup_finalization_writes_then_signs_fail_closed(backup_src: str) -> None:
    assert 'promote_new_file "$tmp" "${manifest}.hmac"' in backup_src
    finalize = backup_src.split("finalize_backup() {", 1)[1].split("\n}\n", 1)[0]
    assert finalize.index("if ! write_manifest; then") < finalize.index(
        "if ! publish_manifest_signature; then"
    )
    assert "\nif ! finalize_backup; then" in backup_src


def test_backup_arms_the_requirement_only_after_a_signature_exists(backup_src: str) -> None:
    publish = backup_src.split("publish_manifest_signature() {", 1)[1].split("\n}\n", 1)[0]
    assert publish.index("sign_manifest") < publish.index("MANIFEST_HMAC_MARKER")


def test_backup_signature_permission_failure_is_fatal(tmp_path: Path) -> None:
    key = tmp_path / "backup_key"
    key.write_text("a-backup-encryption-key\n")
    manifest = tmp_path / f"manifest_{TS}.json"
    manifest.write_text(f'{{"timestamp":"{TS}","archives":[]}}')
    result = _run_bash(
        "source scripts/backup.sh --functions-only;"
        f' ENC_KEYFILE="{key}"; chmod() {{ return 1; }}; sign_manifest "{manifest}"'
    )
    assert result.returncode != 0
    assert not (tmp_path / f"manifest_{TS}.json.hmac").exists()
    assert not (tmp_path / f"manifest_{TS}.json.hmac.tmp").exists()


def test_backup_retention_prunes_manifest_signatures(backup_src: str) -> None:
    assert backup_src.count("manifest_*.json.hmac") == 1
    keep_last = backup_src.split("retention_keep_last_n() {", 1)[1].split("\n}\n", 1)[0]
    assert '-name "manifest_${ts}.json.hmac"' in keep_last


def test_production_backup_without_key_fails_before_any_dump(tmp_path: Path) -> None:
    """A production key error must not leave plaintext database archives."""
    backup_dir = tmp_path / "backups"
    trigger_dir = tmp_path / "triggers"
    host_secrets = tmp_path / "host-secrets"
    secrets_dir = tmp_path / "secrets"
    bin_dir = tmp_path / "bin"
    for directory in (backup_dir, trigger_dir, host_secrets, secrets_dir, bin_dir):
        directory.mkdir()

    postgres_password = tmp_path / "postgres-password"
    postgres_password.write_text("fixture-password", encoding="utf-8")
    empty_backup_key = tmp_path / "empty-backup-key"
    empty_backup_key.touch()
    pg_dump_trace = tmp_path / "pg-dump-ran"
    pg_dump_stub = bin_dir / "pg_dump"
    pg_dump_stub.write_text(
        f"#!/usr/bin/env bash\nprintf ran > {pg_dump_trace}\nprintf SQL\n",
        encoding="utf-8",
    )
    pg_dump_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(BACKUP_SH)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ENVIRONMENT": "production",
            "POSTGRES_PASSWORD_FILE": str(postgres_password),
            "BACKUP_ENCRYPT_KEYFILE": str(empty_backup_key),
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_TRIGGER_DIR": str(trigger_dir),
            "HOST_SECRETS_DIR": str(host_secrets),
            "SECRETS_DIR": str(secrets_dir),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "BACKUP_ENCRYPT_KEYFILE" in result.stderr
    assert "production" in result.stderr
    assert not pg_dump_trace.exists(), "pg_dump ran before the production key refusal"
    assert not list(backup_dir.glob("jarvis_*.sql.gz*"))
    assert not list(backup_dir.glob("litellm_*.sql.gz*"))
    status = json.loads((backup_dir / ".last_run.json").read_text(encoding="utf-8"))
    assert status["succeeded"] is False
    assert status["stores"]["jarvis"] == "failed"
    assert status["stores"]["litellm"] == "failed"


def test_backup_without_key_refuses_outside_production_and_records_the_failure(
    tmp_path: Path,
) -> None:
    """A set with no key cannot be restored, so no environment may report it as taken."""
    backup_dir = tmp_path / "backups"
    trigger_dir = tmp_path / "triggers"
    host_secrets = tmp_path / "host-secrets"
    secrets_dir = tmp_path / "secrets"
    bin_dir = tmp_path / "bin"
    for directory in (backup_dir, trigger_dir, host_secrets, secrets_dir, bin_dir):
        directory.mkdir()

    postgres_password = tmp_path / "postgres-password"
    postgres_password.write_text("fixture-password", encoding="utf-8")
    empty_backup_key = tmp_path / "empty-backup-key"
    empty_backup_key.touch()
    pg_dump_trace = tmp_path / "pg-dump-ran"
    pg_dump_stub = bin_dir / "pg_dump"
    pg_dump_stub.write_text(
        f"#!/usr/bin/env bash\nprintf ran > {pg_dump_trace}\nprintf SQL\n",
        encoding="utf-8",
    )
    pg_dump_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(BACKUP_SH)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ENVIRONMENT": "development",
            "POSTGRES_PASSWORD_FILE": str(postgres_password),
            "BACKUP_ENCRYPT_KEYFILE": str(empty_backup_key),
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_TRIGGER_DIR": str(trigger_dir),
            "HOST_SECRETS_DIR": str(host_secrets),
            "SECRETS_DIR": str(secrets_dir),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "a backup taken without a key cannot be restored" in result.stderr
    assert str(empty_backup_key) in result.stderr
    assert not pg_dump_trace.exists(), "pg_dump ran before the keyless refusal"
    assert not list(backup_dir.glob("jarvis_*.sql.gz*"))
    assert not list(backup_dir.glob("secrets_*.tar.gz*"))
    # The refusal sits after the EXIT trap is installed: a run that cannot produce a
    # restorable set must say so, not leave /status reporting the last success.
    status = json.loads((backup_dir / ".last_run.json").read_text(encoding="utf-8"))
    assert status["succeeded"] is False
    assert status["encrypted"] is False
    assert status["stores"]["secrets"] == "skipped"


def _complete_backup_run(
    tmp_path: Path,
    *,
    aws_stub: str | None,
    env: dict[str, str] | None = None,
    before_run: Callable[[Path], None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Drive backup.sh end to end over stubbed tools and return its result plus backup dir.

    Only the vector store is absent (nothing listens on the Qdrant URL), so the run
    produces a complete local set and reaches the off-site upload block. ``aws_stub``
    is the body of a fake ``aws`` executable, or ``None`` to leave the CLI missing.
    ``before_run`` seeds the backup directory before the script starts.
    """
    backup_dir = tmp_path / "backups"
    trigger_dir = tmp_path / "triggers"
    host_secrets = tmp_path / "host-secrets"
    secrets_dir = tmp_path / "secrets"
    pdf_dir = tmp_path / "pdfs"
    bin_dir = tmp_path / "bin"
    for directory in (backup_dir, trigger_dir, host_secrets, secrets_dir, pdf_dir, bin_dir):
        directory.mkdir()

    postgres_password = tmp_path / "postgres-password"
    postgres_password.write_text("fixture-password", encoding="utf-8")
    backup_key = tmp_path / "backup-key"
    backup_key.write_text("fixture-backup-key", encoding="utf-8")
    for data_key in (
        "jarvis_config_key.txt",
        "jarvis_model_hmac_key.txt",
        "litellm_salt_key.txt",
    ):
        (secrets_dir / data_key).write_text("fixture-key-material", encoding="utf-8")
    (pdf_dir / "1.pdf").write_text("%PDF-fixture", encoding="utf-8")

    if before_run is not None:
        before_run(backup_dir)

    for name, body in (
        ("pg_dump", "#!/usr/bin/env bash\nprintf 'SQL FIXTURE DUMP\\n'\n"),
        ("psql", "#!/usr/bin/env bash\nprintf '102\\n'\n"),
    ):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    if aws_stub is not None:
        stub = bin_dir / "aws"
        stub.write_text(aws_stub, encoding="utf-8")
        stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(BACKUP_SH)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ENVIRONMENT": "development",
            "POSTGRES_PASSWORD_FILE": str(postgres_password),
            "BACKUP_ENCRYPT_KEYFILE": str(backup_key),
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_TRIGGER_DIR": str(trigger_dir),
            "HOST_SECRETS_DIR": str(host_secrets),
            "SECRETS_DIR": str(secrets_dir),
            "PDF_STORAGE_DIR": str(pdf_dir),
            # No Qdrant listens here, so the run records an unreachable vector store.
            "QDRANT_URL": "http://127.0.0.1:1",
            "BACKUP_SKIP_PRUNE": "1",
            **(env or {}),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result, backup_dir


def _last_run(backup_dir: Path) -> dict:
    return json.loads((backup_dir / ".last_run.json").read_text(encoding="utf-8"))


def test_backup_records_an_unreachable_vector_store_without_failing_the_run(
    tmp_path: Path,
) -> None:
    """A vector outage is frequently why an operator restores; it must not block one."""
    result, backup_dir = _complete_backup_run(tmp_path, aws_stub=None)

    assert result.returncode == 0, result.stderr
    status = _last_run(backup_dir)
    assert status["succeeded"] is True
    assert status["run_exit_code"] == 0
    assert status["stores"]["qdrant"] == "unreachable"
    assert status["vectors_captured"] is False
    # No bucket is configured, so nothing was promised off-site and nothing failed.
    assert status["s3_complete"] is True


def test_backup_reports_a_failed_off_site_upload_without_changing_its_exit_status(
    tmp_path: Path,
) -> None:
    """restore.sh gates the safety pre-backup on this exit code before reading succeeded."""
    counter = tmp_path / "aws-calls"
    aws_stub = (
        "#!/usr/bin/env bash\n"
        f"printf x >> {counter}\n"
        f'if [ "$(wc -c < {counter})" -eq 2 ]; then exit 1; fi\n'
        'if [ "$1" = "s3api" ]; then printf 0\\\\n; fi\n'
        "exit 0\n"
    )
    result, backup_dir = _complete_backup_run(
        tmp_path, aws_stub=aws_stub, env={"BACKUP_S3_BUCKET": "fixture-bucket"}
    )

    assert result.returncode == 0, result.stderr
    assert "off-site copy incomplete" in result.stderr
    assert "local backup" in result.stderr and "complete and usable" in result.stderr
    assert "FATAL" not in result.stderr
    status = _last_run(backup_dir)
    assert status["succeeded"] is True
    assert status["s3_complete"] is False


def test_backup_refuses_to_call_a_truncated_off_site_copy_complete(tmp_path: Path) -> None:
    """An upload that reported success but arrived short is not an off-site copy."""
    aws_stub = '#!/usr/bin/env bash\nif [ "$1" = "s3api" ]; then printf \'1\\n\'; fi\nexit 0\n'
    result, backup_dir = _complete_backup_run(
        tmp_path, aws_stub=aws_stub, env={"BACKUP_S3_BUCKET": "fixture-bucket"}
    )

    assert result.returncode == 0, result.stderr
    assert "does not match the local archive size" in result.stderr
    status = _last_run(backup_dir)
    assert status["succeeded"] is True
    assert status["s3_complete"] is False


def test_backup_records_a_post_finalization_abort_as_a_complete_set(tmp_path: Path) -> None:
    """The archives are already restorable, so the honest record is success plus the error."""

    def block_the_retention_lock(backup_dir: Path) -> None:
        # A directory where the retention lock file belongs aborts the run during
        # cleanup, after finalize_backup has published the whole restore point.
        (backup_dir / ".lifecycle" / "update.lock").mkdir(parents=True)

    result, backup_dir = _complete_backup_run(
        tmp_path,
        aws_stub=None,
        env={"BACKUP_SKIP_PRUNE": ""},
        before_run=block_the_retention_lock,
    )

    assert result.returncode != 0, result.stdout
    assert list(backup_dir.glob("manifest_*.json.hmac"))
    status = _last_run(backup_dir)
    assert status["succeeded"] is True
    assert status["run_exit_code"] == result.returncode


def test_restore_authenticates_the_manifest_before_the_checksum_gate(restore_src: str) -> None:
    assert restore_src.index("\ngate_manifest_signature\n") < restore_src.index("sha256sum -c")


def test_restore_reports_manual_recovery_only_after_destruction(restore_src: str) -> None:
    assignments = [
        line.strip()
        for line in restore_src.splitlines()
        if "MANUAL_STEPS_REQUIRED=1" in line and not line.lstrip().startswith("#")
    ]
    assert len(assignments) == 1
    assert "DROP_STARTED" in assignments[0]


def test_auth_purge_is_transactional_conditional_and_fails_closed(tmp_path: Path) -> None:
    query_log = tmp_path / "auth-purge.sql"
    body = (
        "source scripts/restore.sh --functions-only\n"
        'psql() { cat > "$AUTH_PURGE_QUERY_LOG"; return "${PSQL_RC:-0}"; }\n'
        "purge_restored_auth_state jarvis_restore_tmp\n"
    )

    result = _run_bash(body, env={"AUTH_PURGE_QUERY_LOG": str(query_log)})

    assert result.returncode == 0, result.stderr
    query = query_log.read_text()
    assert "BEGIN" in query
    assert "COMMIT" in query
    ephemeral_tables = (
        "sessions",
        "magic_link_tokens",
        "webauthn_challenges",
        "telegram_pairing_tokens",
    )
    for table in ephemeral_tables:
        assert table in query
        assert "to_regclass" in query
    for table in ("users", "webauthn_credentials", "telegram_user_pairings"):
        assert table not in query

    failed = _run_bash(body, env={"AUTH_PURGE_QUERY_LOG": str(query_log), "PSQL_RC": "1"})
    assert failed.returncode != 0


def test_auth_purge_runs_before_the_destructive_marker(restore_src: str) -> None:
    verify = restore_src.index('verify_db_structural "$tmp" "$is_jarvis"')
    marker = restore_src.index('touch "$MAINTENANCE_DESTRUCTIVE"')
    purge = restore_src.index('purge_restored_auth_state "$tmp"')

    assert verify < purge < marker


@pytest.mark.parametrize(
    ("step_status", "recorded_outcome"),
    [("done", "succeeded"), ("degraded", "degraded"), ("skipped", "skipped")],
)
def test_vector_visibility_checkpoint_rotation_records_qdrant_outcome(
    tmp_path: Path,
    step_status: str,
    recorded_outcome: str,
) -> None:
    sql_log = tmp_path / "visibility-checkpoint.sql"
    args_log = tmp_path / "visibility-checkpoint.args"
    generation = "0123456789abcdef0123456789abcdef"
    body = (
        "source scripts/restore.sh --functions-only\n"
        f"STEP_QDRANT={step_status}\n"
        f"openssl() {{ printf '{generation}'; }}\n"
        'psql() { printf \'%s\\n\' "$*" > "$CHECKPOINT_ARGS_LOG"; '
        'cat > "$CHECKPOINT_SQL_LOG"; }\n'
        'rotate_vector_visibility_checkpoint "$STEP_QDRANT"\n'
        "printf 'GENERATION=%s\\n' \"$VECTOR_VISIBILITY_GENERATION\"\n"
    )

    result = _run_bash(
        body,
        env={
            "CHECKPOINT_ARGS_LOG": str(args_log),
            "CHECKPOINT_SQL_LOG": str(sql_log),
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"GENERATION={generation}" in result.stdout
    assert f"visibility_generation={generation}" in args_log.read_text()
    assert f"qdrant_recovery={recorded_outcome}" in args_log.read_text()
    sql = sql_log.read_text()
    assert "BEGIN" in sql and "COMMIT" in sql
    assert "vector_visibility.checkpoint" in sql
    assert "visibility_generation" in sql
    assert "last_chunk_id" in sql
    assert "pending" in sql


def test_vector_visibility_checkpoint_refuses_an_invalid_generation(tmp_path: Path) -> None:
    psql_trace = tmp_path / "psql-called"
    body = (
        "source scripts/restore.sh --functions-only\n"
        "openssl() { printf invalid; }\n"
        'psql() { : > "$PSQL_TRACE"; }\n'
        "if rotate_vector_visibility_checkpoint done; then exit 1; fi\n"
        '[ ! -e "$PSQL_TRACE" ]\n'
    )

    result = _run_bash(body, env={"PSQL_TRACE": str(psql_trace)})

    assert result.returncode == 0, result.stderr
    assert not psql_trace.exists()


def test_visibility_rotation_follows_qdrant_and_precedes_clean_completion(
    restore_src: str,
) -> None:
    qdrant_outcome = restore_src.index(
        'if [ "$QDRANT_OK" -eq 1 ]; then STEP_QDRANT="done"; else STEP_QDRANT="degraded"; fi'
    )
    rotation = restore_src.index('rotate_vector_visibility_checkpoint "$STEP_QDRANT"')
    data_keys = restore_src.index('if [ "$DATA_KEYS_STAGED" = "1" ]; then')

    assert qdrant_outcome < rotation < data_keys
    assert (
        'rotate_vector_visibility_checkpoint "$STEP_QDRANT" \\\n  || fail_after_restore'
    ) in restore_src


@pytest.mark.parametrize(
    "marker_name",
    [".maintenance", ".destructive", ".outbound-quarantine.json"],
)
@pytest.mark.parametrize("dangling", [False, True])
def test_litellm_entrypoint_detects_every_restore_hold_entry(
    tmp_path: Path,
    marker_name: str,
    dangling: bool,
) -> None:
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    marker = trigger / marker_name
    if dangling:
        marker.symlink_to(tmp_path / "missing-target")
    else:
        marker.touch()

    result = _run_bash(
        "source scripts/litellm-entrypoint.sh --functions-only\nlitellm_restore_hold_active\n",
        env={"LITELLM_TRIGGER_DIR": str(trigger)},
    )

    assert result.returncode == 0, result.stderr


def test_litellm_healthcheck_allows_dependents_during_restore_review(tmp_path: Path) -> None:
    """A restore-review marker makes the wrapper healthcheck succeed."""
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    (trigger / ".outbound-quarantine.json").touch()

    result = _run_bash(
        "source scripts/litellm-entrypoint.sh --functions-only\nlitellm_healthcheck\n",
        env={"LITELLM_TRIGGER_DIR": str(trigger)},
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("contents", "expected"),
    [("42\n", "42"), ("42\n43\n", "0"), ("not-an-epoch\n", "0")],
)
def test_litellm_rotation_marker_requires_one_numeric_line(
    tmp_path: Path,
    contents: str,
    expected: str,
) -> None:
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    (trigger / ".secrets_rotated").write_text(contents)
    result = _run_bash(
        "source scripts/litellm-entrypoint.sh --functions-only\n"
        "read_rotation_marker\n"
        "printf '%s' \"$rotation_marker_value\"\n",
        env={"LITELLM_TRIGGER_DIR": str(trigger)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_litellm_entrypoint_holds_start_and_stops_a_faux_provider(tmp_path: Path) -> None:
    trigger = tmp_path / "trigger"
    secrets = tmp_path / "secrets"
    bin_dir = tmp_path / "bin"
    trigger.mkdir()
    secrets.mkdir()
    bin_dir.mkdir()
    (trigger / ".maintenance").touch()

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]

    faux_litellm = bin_dir / "litellm"
    faux_litellm.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Server(HTTPServer):\n"
        "    allow_reuse_address = True\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        self.send_response(418)\n"
        "        self.end_headers()\n"
        "    def log_message(self, format, *args):\n"
        "        pass\n"
        "Server(('127.0.0.1', int(os.environ['FAUX_LITELLM_PORT'])), Handler).serve_forever()\n"
    )
    faux_litellm.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LITELLM_TRIGGER_DIR": str(trigger),
        "LITELLM_SECRET_DIR": str(secrets),
        "LITELLM_WATCH_INTERVAL_SECONDS": "0.05",
        "FAUX_LITELLM_PORT": str(port),
    }
    with _litellm_entrypoint_process(env) as proc:
        time.sleep(0.2)
        assert proc.poll() is None, "the entrypoint read missing secrets while restore was held"
        assert not _tcp_accepts(port)

        (secrets / "litellm_master_key").write_text("development-master-key")
        (secrets / "litellm_salt_key").write_text("development-salt-key")
        (secrets / "postgres_password").write_text("development-postgres-password")
        (trigger / ".maintenance").unlink()
        assert _wait_for_tcp(port, accepting=True)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        conn.request("POST", "/v1/chat/completions", body="{}")
        assert conn.getresponse().status == 418
        conn.close()

        (trigger / ".secrets_rotated").write_text("1\n")
        assert _wait_for_tcp(port, accepting=False)
        assert proc.wait(timeout=3) != 0

    with _litellm_entrypoint_process(env) as after_rotation:
        assert _wait_for_tcp(port, accepting=True)
        (trigger / ".outbound-quarantine.json").symlink_to(tmp_path / "missing-review")
        assert _wait_for_tcp(port, accepting=False)
        assert after_rotation.wait(timeout=3) != 0

    with _litellm_entrypoint_process(env) as restarted:
        time.sleep(0.2)
        assert restarted.poll() is None
        assert not _tcp_accepts(port)
        (trigger / ".outbound-quarantine.json").unlink()
        assert _wait_for_tcp(port, accepting=True)


def test_restore_waits_for_two_litellm_connection_failures() -> None:
    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        "probe_calls=0\n"
        "litellm_accepts_http() { probe_calls=$((probe_calls + 1)); return 1; }\n"
        "wait_for_litellm_quarantine\n"
        '[ "$probe_calls" -eq 2 ]\n',
        env={
            "LITELLM_PAUSE_TIMEOUT_SECONDS": "1",
            "LITELLM_PAUSE_POLL_SECONDS": "0.01",
        },
    )

    assert result.returncode == 0, result.stderr


def test_restore_treats_http_503_as_reachable() -> None:
    with _reachable_http_server() as server:
        port = server.server_address[1]
        proc = subprocess.Popen(
            [
                "bash",
                "-c",
                "source scripts/restore.sh --functions-only; wait_for_litellm_quarantine",
            ],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "LITELLM_RESTORE_HOST": "127.0.0.1",
                "LITELLM_RESTORE_PORT": str(port),
                "LITELLM_PAUSE_TIMEOUT_SECONDS": "2",
                "LITELLM_PAUSE_POLL_SECONDS": "0.05",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.2)
        assert proc.poll() is None, "HTTP 503 must still count as reachable"

    try:
        assert proc.wait(timeout=3) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)


def test_restore_times_out_while_litellm_remains_reachable() -> None:
    with _reachable_http_server() as server:
        port = server.server_address[1]
        result = _run_bash(
            "source scripts/restore.sh --functions-only\nwait_for_litellm_quarantine\n",
            env={
                "LITELLM_RESTORE_HOST": "127.0.0.1",
                "LITELLM_RESTORE_PORT": str(port),
                "LITELLM_PAUSE_TIMEOUT_SECONDS": "1",
                "LITELLM_PAUSE_POLL_SECONDS": "0.05",
            },
        )
        assert result.returncode != 0


def test_restore_litellm_probe_configuration_fails_closed() -> None:
    result = _run_bash(
        "source scripts/restore.sh --functions-only\nwait_for_litellm_quarantine\n",
        env={
            "LITELLM_RESTORE_PORT": "not-a-port",
            "LITELLM_PAUSE_TIMEOUT_SECONDS": "1",
            "LITELLM_PAUSE_POLL_SECONDS": "0.05",
        },
    )

    assert result.returncode != 0


def test_restore_stops_litellm_before_backup_or_database_replacement(
    restore_src: str,
) -> None:
    maintenance = restore_src.index('touch "$MAINTENANCE_SENTINEL"')
    pause = restore_src.index("wait_for_litellm_quarantine", maintenance)
    safety_backup = restore_src.index("/usr/local/bin/backup.sh", pause)
    jarvis_swap = restore_src.index('restore_one_db_swap "$JARVIS_DB"', pause)
    litellm_swap = restore_src.index('restore_one_db_swap "$LITELLM_DB"', pause)

    assert maintenance < pause < safety_backup < jarvis_swap < litellm_swap


def test_outbound_quarantine_writer_binds_only_an_off_host_restore(tmp_path: Path) -> None:
    sentinel = tmp_path / ".outbound-quarantine.json"
    restore_id = "0123456789abcdef0123456789abcdef"
    requested_at = "2026-07-21T20:00:00+00:00"
    completed_at = "2026-07-21T20:05:00+00:00"
    body = (
        "source scripts/restore.sh --functions-only\n"
        f'OUTBOUND_QUARANTINE_SENTINEL="{sentinel}"\n'
        f'write_outbound_quarantine {restore_id} inbox "{requested_at}" "{completed_at}"\n'
    )

    result = _run_bash(body)

    assert result.returncode == 0, result.stderr
    assert json.loads(sentinel.read_text()) == {
        "version": 1,
        "restore_id": restore_id,
        "source": "inbox",
        "requested_at": requested_at,
        "completed_at": completed_at,
        "review_state": "awaiting_review",
    }
    assert sentinel.stat().st_mode & 0o777 == 0o644
    assert sentinel.stat().st_nlink == 1

    sentinel.unlink()
    local = _run_bash(body.replace(" inbox ", " local "))
    assert local.returncode != 0
    assert not sentinel.exists()


def test_outbound_quarantine_writer_never_replaces_an_existing_review(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / ".outbound-quarantine.json"
    existing = '{"version":1,"restore_id":"existing"}\n'
    sentinel.write_text(existing)
    body = (
        "source scripts/restore.sh --functions-only\n"
        f'OUTBOUND_QUARANTINE_SENTINEL="{sentinel}"\n'
        "write_outbound_quarantine "
        "0123456789abcdef0123456789abcdef inbox "
        '"2026-07-21T20:00:00+00:00" "2026-07-21T20:05:00+00:00"\n'
    )

    result = _run_bash(body)

    assert result.returncode != 0
    assert sentinel.read_text() == existing


def test_outbound_quarantine_publication_is_flushed_before_success(
    restore_src: str,
) -> None:
    writer_start = restore_src.index("\nwrite_outbound_quarantine() {")
    writer_end = restore_src.index("\n}\n", writer_start)
    writer = restore_src[writer_start:writer_end]

    link = writer.index('ln -- "$tmp" "$OUTBOUND_QUARANTINE_SENTINEL"')
    unlink_temp = writer.index('rm -f -- "$tmp"', link)
    sync_file = writer.index('sync -d "$OUTBOUND_QUARANTINE_SENTINEL"', unlink_temp)
    sync_filesystem = writer.index('sync -f "$(dirname "$OUTBOUND_QUARANTINE_SENTINEL")"')

    assert link < unlink_temp < sync_file < sync_filesystem


def test_outbound_quarantine_presence_check_rejects_dangling_symlinks(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / ".outbound-quarantine.json"
    sentinel.symlink_to(tmp_path / "missing-target")
    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f'OUTBOUND_QUARANTINE_SENTINEL="{sentinel}"\n'
        "outbound_quarantine_exists\n"
    )

    assert result.returncode == 0


def test_restore_request_consumer_requires_a_successful_unlink(tmp_path: Path) -> None:
    request = tmp_path / ".restore_request.json"
    request.write_text('{"request":"one"}')
    body = (
        "source scripts/restore.sh --functions-only\n"
        f'REQUEST_FILE="{request}"\n'
        "rm() { return 1; }\n"
        "consume_restore_request\n"
    )

    result = _run_bash(body)

    assert result.returncode != 0
    assert "command not found" not in result.stderr
    assert request.read_text() == '{"request":"one"}'


def test_main_restore_flow_stops_when_request_consumption_fails(
    restore_src: str,
) -> None:
    consume = 'if ! REQ_CONTENT="$(consume_restore_request)"; then'
    refusal = 'fail_before_destruction "restore request could not be consumed; nothing was changed"'

    assert consume in restore_src
    assert restore_src.index(consume) < restore_src.index(refusal)


def test_restore_request_identity_parser_is_strict() -> None:
    restore_id = "0123456789abcdef0123456789abcdef"
    requested_at = "2026-07-21T20:00:00+00:00"
    valid = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f"parse_restore_identity_request "
        f'\'{{"restore_id":"{restore_id}","requested_at":"{requested_at}"}}\'\n'
        'printf "%s\\t%s" "$RESTORE_ID" "$REQUESTED_AT"\n'
    )

    assert valid.returncode == 0, valid.stderr
    assert valid.stdout == f"{restore_id}\t{requested_at}"

    missing = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        'parse_restore_identity_request \'{"requested_at":"2026-07-21T20:00:00+00:00"}\'\n'
    )
    assert missing.returncode != 0


@pytest.mark.parametrize(
    "request_body",
    [
        '{"restore_id":"short","requested_at":"2026-07-21T20:00:00+00:00"}',
        '{"restore_id":"0123456789abcdef0123456789abcdef","requested_at":"2026-07-21"}',
        '{"restore_id":"0123456789abcdef0123456789abcdef",'
        '"restore_id":"fedcba9876543210fedcba9876543210",'
        '"requested_at":"2026-07-21T20:00:00+00:00"}',
        '["0123456789abcdef0123456789abcdef","2026-07-21T20:00:00+00:00"]',
        "not json",
    ],
)
def test_restore_request_identity_parser_rejects_malformed_or_ambiguous_json(
    request_body: str,
) -> None:
    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f"parse_restore_identity_request '{request_body}'\n"
    )

    assert result.returncode != 0


def test_off_host_quarantine_is_written_before_clean_completion(restore_src: str) -> None:
    quarantine = restore_src.index(
        'write_outbound_quarantine "$RESTORE_ID" "$SOURCE" "$REQUESTED_AT"'
    )
    clean = restore_src.index("RESTORE_CLEAN=1", quarantine)

    assert quarantine < clean


def test_restore_refuses_a_request_while_outbound_review_is_pending(
    restore_src: str,
) -> None:
    identity = restore_src.index('parse_restore_identity_request "$REQ_CONTENT"')
    guard = restore_src.index("if outbound_quarantine_exists; then", identity)
    maintenance = restore_src.index('touch "$MAINTENANCE_SENTINEL"', guard)

    assert identity < guard < maintenance


def test_exit_cleanup_never_consumes_a_later_restore_request(
    restore_src: str,
) -> None:
    cleanup_start = restore_src.index("\n_cleanup() {")
    cleanup_end = restore_src.index("\n# The script's tests", cleanup_start)
    cleanup = restore_src[cleanup_start:cleanup_end]

    assert "REQUEST_FILE" not in cleanup
    assert restore_src.count('rm -f -- "$REQUEST_FILE"') == 1


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH])
def test_the_helper_entry_point_is_a_no_op_when_the_script_is_executed(script: Path) -> None:
    result = subprocess.run(
        ["bash", str(script), "--functions-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_compose_mounts_the_schema_version_baseline() -> None:
    assert "./db/SCHEMA_VERSION:/app/db/SCHEMA_VERSION:ro" in COMPOSE.read_text()


def _write_current_manifest(
    archive_dir: Path,
    *,
    include_pdfs: bool,
    include_secrets: bool = True,
    encrypted: bool = True,
) -> None:
    """Write and sign a current manifest backed by nonempty fixture files."""
    key = archive_dir / "backup_key"
    key.write_text("a-backup-encryption-key\n")
    entries: list[dict[str, object]] = []
    suffix = ".enc" if encrypted else ""
    names = [f"jarvis_{TS}.sql.gz{suffix}", f"litellm_{TS}.sql.gz{suffix}"]
    if include_secrets:
        names.append(f"secrets_{TS}.tar.gz{suffix}")
    if include_pdfs:
        names.append(f"pdfs_{TS}.tar.gz{suffix}")
    for name in names:
        content = f"fixture:{name}".encode()
        (archive_dir / name).write_bytes(content)
        entries.append(
            {
                "filename": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    (archive_dir / f"manifest_{TS}.json").write_text(
        json.dumps(
            {
                "timestamp": TS,
                "run_id": "0123456789abcdef0123456789abcdef",
                "app_version": "1.2.0",
                "schema_version": 106,
                "created_at": "2026-07-19T12:00:00+00:00",
                "archives": entries,
            },
            separators=(",", ":"),
        )
    )
    _sign_manifest(archive_dir)


def test_current_authenticated_manifest_requires_exactly_one_pdf_archive(tmp_path: Path) -> None:
    _write_current_manifest(tmp_path, include_pdfs=False)
    assert "ACCEPTED" not in _authenticate_and_verify_inventory(tmp_path)

    _write_current_manifest(tmp_path, include_pdfs=True)
    assert "ACCEPTED" in _authenticate_and_verify_inventory(tmp_path)


def test_current_unencrypted_local_manifest_may_omit_data_keys_but_encrypted_may_not(
    tmp_path: Path,
) -> None:
    unencrypted = tmp_path / "unencrypted"
    encrypted = tmp_path / "encrypted"
    unencrypted.mkdir()
    encrypted.mkdir()
    _write_current_manifest(
        unencrypted,
        include_pdfs=True,
        include_secrets=False,
        encrypted=False,
    )
    assert "ACCEPTED" in _authenticate_and_verify_inventory(unencrypted)

    _write_current_manifest(
        encrypted,
        include_pdfs=True,
        include_secrets=False,
        encrypted=True,
    )
    assert "ACCEPTED" not in _authenticate_and_verify_inventory(encrypted)


def test_off_host_safety_backup_uses_the_target_hosts_backup_key(tmp_path: Path) -> None:
    """The incoming archive key must not authenticate the destination safety backup."""
    _write_current_manifest(tmp_path, include_pdfs=True)
    source_key = tmp_path / "source_operator_key"
    source_key.write_text("different-source-backup-key\n")
    run_id = "0123456789abcdef0123456789abcdef"
    (tmp_path / ".last_run.json").write_text(
        json.dumps(
            {
                "timestamp": TS,
                "run_id": run_id,
                "succeeded": True,
            },
            separators=(",", ":"),
        )
    )

    result = _run_bash(
        f'BACKUP_ENCRYPT_KEYFILE="{tmp_path / "backup_key"}"\n'
        "source scripts/restore.sh --functions-only\n"
        f'BACKUP_DIR="{tmp_path}"\n'
        f'ENC_KEYFILE="{source_key}"\n'
        f'safety_backup_is_fresh 0 "{run_id}"\n'
        '[ -d "$SAFETY_STAGING_DIR" ]\n'
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("request_json", "expected", "accepted"),
    [
        ('{"timestamp":"x"}', 0, True),
        ('{"allow_missing_pdfs":false}', 0, True),
        ('{"allow_missing_pdfs":true}', 1, True),
        ('{"allow_missing_pdfs":"true"}', 0, False),
        ('{"allow_missing_pdfs":true,"allow_missing_pdfs":false}', 0, False),
    ],
)
def test_restore_request_parses_the_legacy_pdf_consent_once(
    request_json: str,
    expected: int,
    accepted: bool,
) -> None:
    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f"REQ_CONTENT='{request_json}'\n"
        'if parse_allow_missing_pdfs_request "$REQ_CONTENT"; then '
        "printf 'ACCEPT:%s' \"$ALLOW_MISSING_PDFS\"; else printf REJECT; fi\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (f"ACCEPT:{expected}" if accepted else "REJECT")


@pytest.mark.parametrize(
    ("consent", "authenticated", "legacy", "allowed"),
    [
        (1, 1, 1, True),
        (0, 1, 1, False),
        (1, 0, 1, False),
        (1, 1, 0, False),
    ],
)
def test_missing_pdf_restore_requires_consent_and_an_authenticated_legacy_manifest(
    consent: int,
    authenticated: int,
    legacy: int,
    allowed: bool,
) -> None:
    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f"ALLOW_MISSING_PDFS={consent}\n"
        f"MANIFEST_AUTHENTICATED={authenticated}\n"
        f"MANIFEST_LEGACY={legacy}\n"
        "missing_pdf_restore_is_authorized\n"
    )
    assert (result.returncode == 0) is allowed


def _run_inbox_manifest(inbox: Path, trigger: Path) -> list[dict[str, object]]:
    result = subprocess.run(
        ["bash", str(RESTORE_SH), "--inbox-manifest"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "RESTORE_INBOX_DIR": str(inbox),
            "BACKUP_TRIGGER_DIR": str(trigger),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads((trigger / ".inbox_manifest.json").read_text())


def test_inbox_inventory_marks_only_authenticated_pre_v12_sets_as_legacy_pdf_omissions(
    signed_legacy_set: Path,
    tmp_path: Path,
) -> None:
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    (signed_legacy_set / "operator_key").write_bytes(
        (signed_legacy_set / "backup_key").read_bytes()
    )

    points = _run_inbox_manifest(signed_legacy_set, trigger)

    assert points == [
        {
            "timestamp": TS,
            "complete": True,
            "has_pdfs": False,
            "legacy_missing_pdfs": True,
            "has_secrets": True,
            "has_key": True,
        }
    ]


def test_inbox_inventory_blocks_a_current_manifest_that_is_missing_pdfs(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    trigger = tmp_path / "trigger"
    inbox.mkdir()
    trigger.mkdir()
    _write_current_manifest(inbox, include_pdfs=False)
    (inbox / "operator_key").write_bytes((inbox / "backup_key").read_bytes())

    points = _run_inbox_manifest(inbox, trigger)

    assert points == [
        {
            "timestamp": TS,
            "complete": False,
            "has_pdfs": False,
            "legacy_missing_pdfs": False,
            "has_secrets": True,
            "has_key": True,
        }
    ]


def test_restore_archive_allowlist_accepts_the_pdf_archive_shape() -> None:
    result = _run_bash(
        f"source scripts/restore.sh --functions-only\nvalid_archive_name pdfs_{TS}.tar.gz.enc\n"
    )
    assert result.returncode == 0, result.stderr


def test_restore_data_key_allowlist_is_exact() -> None:
    allowed = {
        "jarvis_config_key.txt",
        "jarvis_model_hmac_key.txt",
        "litellm_salt_key.txt",
    }
    candidates = allowed | {
        "postgres_password.txt",
        "jarvis_api_key.txt",
        "litellm_master_key.txt",
        "telegram_bot_token.txt",
        "smtp_pass.txt",
        "cloudflare_tunnel_token.txt",
        "unknown_future_secret.txt",
    }
    body = "source scripts/restore.sh --functions-only\n"
    for candidate in sorted(candidates):
        body += (
            f"if restorable_inbox_secret_basename {candidate}; then "
            f"printf 'ALLOW {candidate}\\n'; else printf 'DENY {candidate}\\n'; fi\n"
        )
    result = _run_bash(body)
    assert result.returncode == 0, result.stderr
    observed = {
        line.split(" ", 1)[1] for line in result.stdout.splitlines() if line.startswith("ALLOW ")
    }
    assert observed == allowed


def test_restore_keeps_the_target_postgres_role_password(restore_src: str) -> None:
    executable = "\n".join(
        line for line in restore_src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ALTER ROLE" not in executable


def test_swap_journal_write_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing" / "swap-state.json"
    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f'SWAP_STATE_FILE="{missing_parent}"\n'
        "write_swap_state jarvis reload_tmp\n"
    )
    assert result.returncode != 0


def test_new_secret_archive_is_limited_to_the_three_data_keys(backup_src: str) -> None:
    block = backup_src.split("# --- secrets/ directory", 1)[1].split(
        "# --- Qdrant vector store", 1
    )[0]
    for filename in (
        "jarvis_config_key.txt",
        "jarvis_model_hmac_key.txt",
        "litellm_salt_key.txt",
    ):
        assert filename in block
    assert "postgres_password.txt" not in block
    assert "telegram_bot_token.txt" not in block
    assert "      ." not in block


def _run_pdf_backup(pdf_dir: Path, backup_dir: Path) -> subprocess.CompletedProcess:
    return _run_bash(
        "source scripts/backup.sh --functions-only\n"
        f'PDF_STORAGE_DIR="{pdf_dir}"\n'
        f'BACKUP_DIR="{backup_dir}"\n'
        f"TIMESTAMP={TS}\n"
        "ENCRYPT=0\n"
        "encrypt_or_passthrough() { cat; }\n"
        "backup_pdfs\n"
    )


def test_pdf_backup_is_complete_for_empty_and_nonempty_numeric_object_stores(
    tmp_path: Path,
) -> None:
    pdf_dir = tmp_path / "pdfs"
    backup_dir = tmp_path / "backups"
    pdf_dir.mkdir()
    backup_dir.mkdir()

    empty = _run_pdf_backup(pdf_dir, backup_dir)
    assert empty.returncode == 0, empty.stderr
    archive = backup_dir / f"pdfs_{TS}.tar.gz"
    with tarfile.open(archive, "r:gz") as tar:
        assert tar.getnames() == []

    archive.unlink()
    (pdf_dir / "1.pdf").write_bytes(b"one")
    (pdf_dir / "002.pdf").write_bytes(b"two")
    (pdf_dir / "draft.pdf").write_bytes(b"not-published")
    (pdf_dir / ".upload.tmp").write_bytes(b"partial")

    populated = _run_pdf_backup(pdf_dir, backup_dir)
    assert populated.returncode == 0, populated.stderr
    with tarfile.open(archive, "r:gz") as tar:
        assert sorted(tar.getnames()) == ["002.pdf", "1.pdf"]
        assert tar.extractfile("1.pdf").read() == b"one"
        assert tar.extractfile("002.pdf").read() == b"two"


def test_pdf_backup_refuses_a_numeric_symlink(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "pdfs"
    backup_dir = tmp_path / "backups"
    pdf_dir.mkdir()
    backup_dir.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (pdf_dir / "7.pdf").symlink_to(outside)

    result = _run_pdf_backup(pdf_dir, backup_dir)

    assert result.returncode != 0
    assert "not a safe regular file" in result.stderr
    assert not (backup_dir / f"pdfs_{TS}.tar.gz").exists()


def test_pdf_backup_uses_a_read_only_publish_lock(tmp_path: Path) -> None:
    """The sidecar must interoperate with an app-owned, non-writable lock file."""
    pdf_dir = tmp_path / "pdfs"
    backup_dir = tmp_path / "backups"
    pdf_dir.mkdir()
    backup_dir.mkdir()
    (pdf_dir / "1.pdf").write_bytes(b"one")
    lock = pdf_dir / ".publish.lock"
    lock.touch(mode=0o444)

    result = _run_pdf_backup(pdf_dir, backup_dir)

    assert result.returncode == 0, result.stderr
    assert (backup_dir / f"pdfs_{TS}.tar.gz").exists()


def _data_key_payloads() -> dict[str, bytes]:
    return {
        "jarvis_config_key.txt": base64.urlsafe_b64encode(b"c" * 32),
        "jarvis_model_hmac_key.txt": b"h" * 64,
        "litellm_salt_key.txt": b"s" * 64,
    }


def _old_data_key_payloads() -> dict[str, bytes]:
    return {
        "jarvis_config_key.txt": base64.urlsafe_b64encode(b"o" * 32),
        "jarvis_model_hmac_key.txt": b"m" * 64,
        "litellm_salt_key.txt": b"t" * 64,
    }


def _write_key_tar(
    path: Path,
    members: list[tuple[str, bytes | None, str]],
) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, payload, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                assert payload is not None
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))


def _stage_data_keys(archive: Path, staging: Path, *, exact: bool) -> subprocess.CompletedProcess:
    return _run_bash(
        "source scripts/restore.sh --functions-only\n"
        'ENC_KEYFILE=""\n'
        f'SECRETS_STAGING="{staging}"\n'
        f'stage_restored_data_keys "{archive}" {1 if exact else 0}\n'
    )


def test_data_key_preflight_accepts_current_exact_and_legacy_broad_archives(
    tmp_path: Path,
) -> None:
    payloads = _data_key_payloads()
    current = tmp_path / "current.tar.gz"
    _write_key_tar(current, [(name, value, "file") for name, value in payloads.items()])
    current_stage = tmp_path / "current-stage"

    exact = _stage_data_keys(current, current_stage, exact=True)

    assert exact.returncode == 0, exact.stderr
    assert {p.name for p in current_stage.iterdir() if not p.name.startswith(".")} == set(payloads)

    legacy = tmp_path / "legacy.tar.gz"
    _write_key_tar(
        legacy,
        [(name, value, "file") for name, value in payloads.items()]
        + [("postgres_password.txt", b"source-host-password", "file")],
    )
    legacy_stage = tmp_path / "legacy-stage"

    broad = _stage_data_keys(legacy, legacy_stage, exact=False)

    assert broad.returncode == 0, broad.stderr
    assert not (legacy_stage / "postgres_password.txt").exists()
    assert {p.name for p in legacy_stage.iterdir() if not p.name.startswith(".")} == set(payloads)
    assert _stage_data_keys(legacy, tmp_path / "exact-stage", exact=True).returncode != 0


@pytest.mark.parametrize("case", ["missing", "duplicate", "symlink", "oversized", "bad_config"])
def test_data_key_preflight_rejects_unsafe_or_incomplete_archives(
    tmp_path: Path,
    case: str,
) -> None:
    payloads = _data_key_payloads()
    members = [(name, value, "file") for name, value in payloads.items()]
    if case == "missing":
        members = members[:-1]
    elif case == "duplicate":
        members.append(("jarvis_config_key.txt", payloads["jarvis_config_key.txt"], "file"))
    elif case == "symlink":
        members = [member for member in members if member[0] != "jarvis_config_key.txt"]
        members.append(("jarvis_config_key.txt", None, "symlink"))
    elif case == "oversized":
        members = [
            (name, b"x" * 4097 if name == "litellm_salt_key.txt" else value, kind)
            for name, value, kind in members
        ]
    else:
        members = [
            (name, b"not-a-fernet-key" if name == "jarvis_config_key.txt" else value, kind)
            for name, value, kind in members
        ]
    archive = tmp_path / f"{case}.tar.gz"
    _write_key_tar(archive, members)

    result = _stage_data_keys(archive, tmp_path / "stage", exact=True)

    assert result.returncode != 0


def test_installing_restored_data_keys_preserves_target_host_credentials(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    host = tmp_path / "host-secrets"
    trigger = tmp_path / "trigger"
    lock_dir = tmp_path / "lifecycle"
    staging.mkdir()
    host.mkdir()
    trigger.mkdir()
    lock_dir.mkdir()
    payloads = _data_key_payloads()
    for name, value in payloads.items():
        (staging / name).write_bytes(value)
    for name, value in _old_data_key_payloads().items():
        (host / name).write_bytes(value)
    target_credentials = {
        "postgres_password.txt": b"target-postgres",
        "jarvis_api_key.txt": b"target-api-key",
        "telegram_bot_token.txt": b"target-telegram",
    }
    for name, value in target_credentials.items():
        (host / name).write_bytes(value)

    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f'SECRETS_STAGING="{staging}"\n'
        f'HOST_SECRETS_DIR="{host}"\n'
        f'TRIGGER_DIR="{trigger}"\n'
        f'LOCK_DIR="{lock_dir}"\n'
        "install_restored_data_keys\n"
    )

    assert result.returncode == 0, result.stderr
    for name, value in payloads.items():
        assert (host / name).read_bytes() == value
        assert (host / name).stat().st_mode & 0o777 == 0o644
    for name, value in target_credentials.items():
        assert (host / name).read_bytes() == value
    assert (trigger / ".secrets_rotated").exists()
    assert not (lock_dir / "data-key-restore").exists()


def test_data_key_install_rolls_back_the_whole_set_after_second_replace_fails(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    host = tmp_path / "host"
    trigger = tmp_path / "trigger"
    lock_dir = tmp_path / "lifecycle"
    shim_dir = tmp_path / "bin"
    for path in (staging, host, trigger, lock_dir, shim_dir):
        path.mkdir()
    new_values = _data_key_payloads()
    old_values = _old_data_key_payloads()
    for name, value in new_values.items():
        (staging / name).write_bytes(value)
    for name, value in old_values.items():
        (host / name).write_bytes(value)

    real_mv = subprocess.run(
        ["bash", "-c", "command -v mv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    mv_shim = shim_dir / "mv"
    mv_shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'dest="${@: -1}"\n'
        'if [[ "$dest" == */jarvis_model_hmac_key.txt ]] && '
        '[ ! -e "$FAIL_ONCE" ]; then\n'
        '  : > "$FAIL_ONCE"\n'
        "  exit 73\n"
        "fi\n"
        f'exec "{real_mv}" "$@"\n'
    )
    mv_shim.chmod(0o755)

    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f'SECRETS_STAGING="{staging}"\n'
        f'HOST_SECRETS_DIR="{host}"\n'
        f'TRIGGER_DIR="{trigger}"\n'
        f'LOCK_DIR="{lock_dir}"\n'
        "install_restored_data_keys\n",
        env={
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "FAIL_ONCE": str(tmp_path / "failed-once"),
        },
    )

    assert result.returncode != 0
    for name, value in old_values.items():
        assert (host / name).read_bytes() == value
    assert not (trigger / ".secrets_rotated").exists()
    assert not (lock_dir / "data-key-restore").exists()


def test_interrupted_data_key_install_recovers_one_complete_new_set(tmp_path: Path) -> None:
    host = tmp_path / "host"
    trigger = tmp_path / "trigger"
    lock_dir = tmp_path / "lifecycle"
    transaction = lock_dir / "data-key-restore"
    old_dir = transaction / "old"
    new_dir = transaction / "new"
    for path in (host, trigger, old_dir, new_dir):
        path.mkdir(parents=True, exist_ok=True)
    new_values = _data_key_payloads()
    old_values = _old_data_key_payloads()
    for name, value in old_values.items():
        (old_dir / name).write_bytes(value)
        (host / name).write_bytes(value)
    for name, value in new_values.items():
        (new_dir / name).write_bytes(value)
    (host / "jarvis_config_key.txt").write_bytes(new_values["jarvis_config_key.txt"])
    (transaction / "state").write_text("installing\n")

    result = _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f'HOST_SECRETS_DIR="{host}"\n'
        f'TRIGGER_DIR="{trigger}"\n'
        f'LOCK_DIR="{lock_dir}"\n'
        "recover_restored_data_keys\n"
    )

    assert result.returncode == 0, result.stderr
    for name, value in new_values.items():
        assert (host / name).read_bytes() == value
    assert (trigger / ".secrets_rotated").exists()
    assert not transaction.exists()


def test_data_keys_are_validated_before_the_safety_backup_and_database_swap(
    restore_src: str,
) -> None:
    call = restore_src.index('stage_restored_data_keys "$SECRETS_ARCHIVE"')
    assert call < restore_src.index("/usr/local/bin/backup.sh")
    assert call < restore_src.index('restore_one_db_swap "$JARVIS_DB"')


def _write_pdf_tar(
    path: Path,
    members: list[tuple[str, bytes | None, str]],
) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, payload, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                assert payload is not None
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))


PDF_RESTORE_RUN_ID = "abcdef0123456789abcdef0123456789"


def _stage_pdfs(
    archive: Path,
    pdf_root: Path,
    *,
    max_files: int = 100,
    max_file_bytes: int = 1024,
    max_total_bytes: int = 4096,
) -> subprocess.CompletedProcess:
    return _run_bash(
        "source scripts/restore.sh --functions-only\n"
        'ENC_KEYFILE=""\n'
        f'PDF_STORAGE_DIR="{pdf_root}"\n'
        f"PDF_RESTORE_MAX_FILES={max_files}\n"
        f"PDF_RESTORE_MAX_FILE_BYTES={max_file_bytes}\n"
        f"PDF_RESTORE_MAX_TOTAL_BYTES={max_total_bytes}\n"
        "PDF_RESTORE_HEADROOM_BYTES=0\n"
        f'stage_restored_pdfs "{archive}" {PDF_RESTORE_RUN_ID}\n'
    )


def test_pdf_restore_preflight_stages_an_exact_empty_or_nonempty_set(tmp_path: Path) -> None:
    for suffix, members, expected in (
        ("empty", [], {}),
        (
            "files",
            [("1.pdf", b"one", "file"), ("20.pdf", b"two", "file")],
            {
                "1.pdf": b"one",
                "20.pdf": b"two",
            },
        ),
    ):
        root = tmp_path / suffix
        root.mkdir()
        archive = tmp_path / f"{suffix}.tar.gz"
        _write_pdf_tar(archive, members)

        result = _stage_pdfs(archive, root)

        assert result.returncode == 0, result.stderr
        stage = root / f".restore-stage-{PDF_RESTORE_RUN_ID}"
        assert stage.is_dir()
        assert {
            path.name: path.read_bytes() for path in stage.iterdir() if path.name.endswith(".pdf")
        } == expected
        assert (stage / ".inventory.tsv").exists()


@pytest.mark.parametrize(
    ("case", "members", "limits"),
    [
        ("traversal", [("../1.pdf", b"escape", "file")], {}),
        ("nested", [("nested/1.pdf", b"nested", "file")], {}),
        ("nonnumeric", [("paper.pdf", b"name", "file")], {}),
        ("symlink", [("1.pdf", None, "symlink")], {}),
        ("duplicate", [("1.pdf", b"one", "file"), ("1.pdf", b"two", "file")], {}),
        ("too_many", [("1.pdf", b"one", "file"), ("2.pdf", b"two", "file")], {"max_files": 1}),
        ("too_large", [("1.pdf", b"12345", "file")], {"max_file_bytes": 4}),
        (
            "too_large_total",
            [("1.pdf", b"123", "file"), ("2.pdf", b"456", "file")],
            {"max_total_bytes": 5},
        ),
    ],
)
def test_pdf_restore_preflight_rejects_hostile_or_unbounded_archives(
    tmp_path: Path,
    case: str,
    members: list[tuple[str, bytes | None, str]],
    limits: dict[str, int],
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    archive = tmp_path / f"{case}.tar.gz"
    _write_pdf_tar(archive, members)

    result = _stage_pdfs(archive, root, **limits)

    assert result.returncode != 0
    assert not (tmp_path / "1.pdf").exists()


def _pdf_restore_shell(pdf_root: Path, body: str) -> subprocess.CompletedProcess:
    lifecycle = pdf_root.parent / "lifecycle"
    lifecycle.mkdir(exist_ok=True)
    return _run_bash(
        "source scripts/restore.sh --functions-only\n"
        f'PDF_STORAGE_DIR="{pdf_root}"\n'
        f'LOCK_DIR="{lifecycle}"\n'
        f'SWAP_STATE_FILE="{lifecycle / "restore-swap-state.json"}"\n'
        f"{body}\n"
    )


def test_pdf_swap_replaces_the_numeric_set_inside_the_stable_bind_root(tmp_path: Path) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    (root / "1.pdf").write_bytes(b"old-one")
    (root / "9.pdf").write_bytes(b"old-nine")
    archive = tmp_path / "new.tar.gz"
    _write_pdf_tar(archive, [("1.pdf", b"new-one", "file"), ("2.pdf", b"new-two", "file")])
    assert _stage_pdfs(archive, root).returncode == 0
    inode_before = root.stat().st_ino

    result = _pdf_restore_shell(root, f"swap_restored_pdfs {PDF_RESTORE_RUN_ID}")

    assert result.returncode == 0, result.stderr
    assert root.stat().st_ino == inode_before
    assert {p.name: p.read_bytes() for p in root.glob("[0-9]*.pdf")} == {
        "1.pdf": b"new-one",
        "2.pdf": b"new-two",
    }
    assert not list(root.glob(".restore-stage-*"))
    assert not list(root.glob(".restore-old-*"))
    assert not (tmp_path / "lifecycle" / "restore-swap-state.json").exists()


def test_pdf_swap_uses_a_read_only_publish_lock(tmp_path: Path) -> None:
    """Restore must not require write permission on the shared lock inode."""
    root = tmp_path / "pdfs"
    root.mkdir()
    (root / "1.pdf").write_bytes(b"old")
    archive = tmp_path / "new.tar.gz"
    _write_pdf_tar(archive, [("1.pdf", b"new", "file")])
    assert _stage_pdfs(archive, root).returncode == 0
    lock = root / ".publish.lock"
    lock.touch(mode=0o444)

    result = _pdf_restore_shell(root, f"swap_restored_pdfs {PDF_RESTORE_RUN_ID}")

    assert result.returncode == 0, result.stderr
    assert (root / "1.pdf").read_bytes() == b"new"


@pytest.mark.parametrize("phase", ["move_old", "move_new", "verify", "cleanup"])
def test_pdf_swap_recovery_completes_forward_from_every_persisted_phase(
    tmp_path: Path,
    phase: str,
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    archive = tmp_path / "new.tar.gz"
    _write_pdf_tar(archive, [("1.pdf", b"new-one", "file"), ("2.pdf", b"new-two", "file")])
    assert _stage_pdfs(archive, root).returncode == 0
    stage = root / f".restore-stage-{PDF_RESTORE_RUN_ID}"
    old = root / f".restore-old-{PDF_RESTORE_RUN_ID}"
    old.mkdir()
    (root / "1.pdf").write_bytes(b"old-one")
    (root / "9.pdf").write_bytes(b"old-nine")

    if phase in {"move_new", "verify", "cleanup"}:
        (root / "1.pdf").replace(old / "1.pdf")
        (root / "9.pdf").replace(old / "9.pdf")
    if phase in {"verify", "cleanup"}:
        (stage / "1.pdf").replace(root / "1.pdf")
        (stage / "2.pdf").replace(root / "2.pdf")

    state = tmp_path / "lifecycle" / "restore-swap-state.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "version": 2,
                "resource": "pdfs",
                "run_id": PDF_RESTORE_RUN_ID,
                "phase": phase,
            },
            separators=(",", ":"),
        )
    )

    result = _pdf_restore_shell(root, "recover_pdf_swap")

    assert result.returncode == 0, result.stderr
    assert {p.name: p.read_bytes() for p in root.glob("[0-9]*.pdf")} == {
        "1.pdf": b"new-one",
        "2.pdf": b"new-two",
    }
    assert not state.exists()


def test_pdf_swap_recovery_refuses_malformed_state_without_deleting_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    state = tmp_path / "lifecycle" / "restore-swap-state.json"
    state.parent.mkdir()
    state.write_text('{"version":2,"resource":"pdfs","run_id":"../../bad","phase":"cleanup"}')

    result = _pdf_restore_shell(root, "recover_pdf_swap")

    assert result.returncode != 0
    assert state.exists()


def test_pdf_swap_cleanup_keeps_recovery_evidence_when_journal_clear_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    archive = tmp_path / "new.tar.gz"
    _write_pdf_tar(archive, [("1.pdf", b"new-one", "file")])
    assert _stage_pdfs(archive, root).returncode == 0
    stage = root / f".restore-stage-{PDF_RESTORE_RUN_ID}"
    old = root / f".restore-old-{PDF_RESTORE_RUN_ID}"
    old.mkdir()
    (stage / "1.pdf").replace(root / "1.pdf")

    lifecycle = tmp_path / "lifecycle"
    lifecycle.mkdir()
    state = lifecycle / "restore-swap-state.json"
    state.write_text(
        json.dumps(
            {
                "version": 2,
                "resource": "pdfs",
                "run_id": PDF_RESTORE_RUN_ID,
                "phase": "cleanup",
            },
            separators=(",", ":"),
        )
    )
    lifecycle.chmod(0o500)
    try:
        result = _run_bash(
            "source scripts/restore.sh --functions-only\n"
            f'PDF_STORAGE_DIR="{root}"\n'
            f'LOCK_DIR="{lifecycle}"\n'
            f'SWAP_STATE_FILE="{state}"\n'
            "recover_pdf_swap\n"
        )
    finally:
        lifecycle.chmod(0o700)

    assert result.returncode != 0
    assert state.exists()
    assert stage.exists()
    assert old.exists()


# --- Signing round-trip -------------------------------------------------------------


def test_a_backup_signature_verifies_in_the_restore_script(signed_set: Path) -> None:
    assert (signed_set / f"manifest_{TS}.json.hmac").read_text().strip() != ""
    assert _verify(signed_set) is True


def test_a_tampered_manifest_fails_verification(signed_set: Path) -> None:
    manifest = signed_set / f"manifest_{TS}.json"
    manifest.write_text(manifest.read_text().replace("104", "105"))
    assert _verify(signed_set) is False


def test_a_wrong_backup_key_fails_verification(signed_set: Path) -> None:
    (signed_set / "other_key").write_text("a-different-key\n")
    assert _verify(signed_set, key_name="other_key") is False


def test_the_signature_is_a_bare_sha256_hex_digest(signed_set: Path) -> None:
    digest = (signed_set / f"manifest_{TS}.json.hmac").read_text().strip()
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


@pytest.mark.parametrize("app_version", ["1.1.3", "1.1.1", "unknown"])
def test_an_authenticated_legacy_manifest_restores_with_its_exact_inventory(
    signed_legacy_set: Path, app_version: str
) -> None:
    manifest = signed_legacy_set / f"manifest_{TS}.json"
    data = json.loads(manifest.read_text())
    data["app_version"] = app_version
    manifest.write_text(json.dumps(data, separators=(",", ":")))
    _sign_manifest(signed_legacy_set)

    assert "ACCEPTED" in _authenticate_and_verify_inventory(signed_legacy_set)


def test_authenticated_legacy_shape_is_narrow_and_fail_closed(
    signed_legacy_set: Path,
) -> None:
    manifest = signed_legacy_set / f"manifest_{TS}.json"
    baseline = json.loads(manifest.read_text())

    def rejected(label: str, data: dict) -> None:
        manifest.write_text(json.dumps(data, separators=(",", ":")))
        _sign_manifest(signed_legacy_set)
        assert "ACCEPTED" not in _authenticate_and_verify_inventory(signed_legacy_set), label

    mutations = []
    changed = json.loads(json.dumps(baseline))
    changed["unexpected"] = True
    mutations.append(("extra root field", changed))
    changed = json.loads(json.dumps(baseline))
    changed["archives"][0]["unexpected"] = True
    mutations.append(("extra archive field", changed))
    changed = json.loads(json.dumps(baseline))
    del changed["created_at"]
    mutations.append(("missing legacy root field", changed))
    changed = json.loads(json.dumps(baseline))
    changed["app_version"] = "1.2.0"
    mutations.append(("v1.2 manifest without run_id", changed))
    changed = json.loads(json.dumps(baseline))
    changed["timestamp"] = "20260719_120001"
    mutations.append(("request and manifest timestamp mismatch", changed))
    changed = json.loads(json.dumps(baseline))
    changed["archives"][0]["filename"] = "jarvis_20260719_120001.sql.gz"
    mutations.append(("renamed archive timestamp", changed))
    changed = json.loads(json.dumps(baseline))
    changed["archives"].append(
        {
            **changed["archives"][0],
            "filename": f"jarvis_{TS}.sql.gz.enc",
        }
    )
    mutations.append(("duplicate logical role", changed))
    changed = json.loads(json.dumps(baseline))
    changed["archives"] = [
        entry for entry in changed["archives"] if not entry["filename"].startswith("litellm_")
    ]
    mutations.append(("missing required role", changed))
    changed = json.loads(json.dumps(baseline))
    changed["archives"][0]["filename"] = f"../jarvis_{TS}.sql.gz"
    mutations.append(("path traversal", changed))
    changed = json.loads(json.dumps(baseline))
    changed["archives"][0]["sha256"] = "0" * 64
    mutations.append(("checksum mismatch", changed))
    changed = json.loads(json.dumps(baseline))
    changed["archives"][0]["size_bytes"] += 1
    mutations.append(("size mismatch", changed))

    for label, changed in mutations:
        rejected(label, changed)


def test_authenticated_legacy_inventory_rejects_unlisted_and_swapped_files(
    signed_legacy_set: Path,
) -> None:
    extra = signed_legacy_set / f"qdrant_unlisted_{TS}.snapshot"
    extra.write_bytes(b"unlisted")
    assert "ACCEPTED" not in _authenticate_and_verify_inventory(signed_legacy_set)
    extra.unlink()

    jarvis = signed_legacy_set / f"jarvis_{TS}.sql.gz"
    original = jarvis.read_bytes()
    target = signed_legacy_set / "swapped-jarvis"
    target.write_bytes(original)
    jarvis.unlink()
    jarvis.symlink_to(target)
    assert "ACCEPTED" not in _authenticate_and_verify_inventory(signed_legacy_set)


def test_legacy_shape_is_not_authenticated_without_a_valid_hmac(
    signed_legacy_set: Path,
) -> None:
    signature = signed_legacy_set / f"manifest_{TS}.json.hmac"
    signature.unlink()
    assert "ACCEPTED" not in _authenticate_and_verify_inventory(signed_legacy_set)

    signature.write_text("0" * 64 + "\n")
    out = _gate(
        signed_legacy_set,
        source="inbox",
        marker=True,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed=f"{BREAK_GLASS_PHRASE}\n",
    )
    assert "PROCEED" not in out
    assert "may have been tampered with" in out


# --- Compatibility matrix -----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "marker"),
    [("local", False), ("local", True), ("inbox", False), ("inbox", True)],
)
def test_a_signed_set_restores_from_either_source(
    signed_set: Path, source: str, marker: bool
) -> None:
    assert "PROCEED" in _gate(signed_set, source=source, marker=marker)
    assert (signed_set / OPENSSL_TRACE).exists(), "a present signature must always be verified"


@pytest.mark.parametrize("source", ["local", "inbox"])
def test_an_invalid_signature_is_refused_even_before_the_ratchet(
    signed_set: Path, source: str
) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").write_text("0" * 64 + "\n")
    out = _gate(signed_set, source=source, marker=False)
    assert "PROCEED" not in out
    assert "may have been tampered with" in out


def test_an_unsigned_local_set_is_allowed_with_a_warning_before_the_ratchet(
    signed_set: Path,
) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(signed_set, source="local", marker=False)
    assert "PROCEED" in out
    assert "WARNING" in out
    assert "cannot be checked for tampering" in out


def test_an_unsigned_local_set_is_refused_once_the_ratchet_is_armed(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(signed_set, source="local", marker=True)
    assert "PROCEED" not in out
    assert "no authenticated manifest" in out


@pytest.mark.parametrize("marker", [False, True])
def test_an_unsigned_off_host_set_is_always_refused(signed_set: Path, marker: bool) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(signed_set, source="inbox", marker=marker)
    assert "PROCEED" not in out
    assert "no authenticated manifest" in out


@pytest.mark.parametrize("source", ["local", "inbox"])
def test_a_bad_signature_is_refused_before_any_destructive_step(
    signed_set: Path, source: str
) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").write_text("0" * 64 + "\n")
    out = _gate(signed_set, source=source, marker=True)
    assert "PROCEED" not in out
    assert "may have been tampered with" in out
    assert "nothing was changed" in out


def test_an_unencrypted_deployment_skips_verification_symmetrically(tmp_path: Path) -> None:
    manifest = tmp_path / f"manifest_{TS}.json"
    manifest.write_text('{"archives":[]}')
    (tmp_path / "backup_key").write_text("")
    (tmp_path / "marker").touch()
    assert "PROCEED" in _gate(tmp_path, source="local", marker=True)


# --- Break-glass --------------------------------------------------------------------


# The override exists for ONE disaster: on this host, signing has arrived (the
# marker is present, so a signature is required) and the only surviving set
# predates it. Every case below is therefore same-host with the marker present.
# An off-host set is refused before the override is even considered, which the
# last test in this block pins.


def test_break_glass_refuses_on_the_environment_variable_alone(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(
        signed_set,
        source="local",
        marker=True,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed="\n",
    )
    assert "PROCEED" not in out
    assert "no authenticated manifest" in out


def test_break_glass_refuses_on_the_typed_phrase_alone(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(signed_set, source="local", marker=True, typed=f"{BREAK_GLASS_PHRASE}\n")
    assert "PROCEED" not in out
    assert "to restore it anyway" not in out, "the prompt must not appear without the override"


def test_break_glass_proceeds_with_a_warning_when_both_are_supplied(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(
        signed_set,
        source="local",
        marker=True,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed=f"{BREAK_GLASS_PHRASE}\n",
    )
    assert "PROCEED" in out
    assert "WITHOUT manifest authentication" in out


def test_break_glass_never_excuses_a_failed_verification(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").write_text("0" * 64 + "\n")
    out = _gate(
        signed_set,
        source="local",
        marker=True,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed=f"{BREAK_GLASS_PHRASE}\n",
    )
    assert "PROCEED" not in out
    assert "may have been tampered with" in out


def test_break_glass_is_unreachable_without_an_interactive_terminal(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(signed_set, source="local", marker=True, env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"})
    assert "PROCEED" not in out
    assert "no authenticated manifest" in out


def test_break_glass_never_accepts_an_off_host_set(signed_set: Path) -> None:
    """On a fresh host there is nothing to check the archives against, so the
    override is refused even when both of its halves are supplied."""
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(
        signed_set,
        source="inbox",
        marker=False,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed=f"{BREAK_GLASS_PHRASE}\n",
    )
    assert "PROCEED" not in out
    assert "no authenticated manifest" in out
