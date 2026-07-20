"""Disaster-recovery script invariants.

Covers the manifest-authentication contract shared by scripts/backup.sh and
scripts/restore.sh, restore.sh's manual-recovery reporting, and the compose mounts the
restore compatibility gate depends on.

The manifest carries the sha256 of every archive, so restore.sh's checksum gate is only
as trustworthy as the manifest itself. backup.sh signs the manifest and restore.sh
verifies that signature before it reads any digest out of it. Both scripts expose their
helpers under `--functions-only`, so the tests below exercise the real openssl
invocations rather than a reimplementation of them.
"""

import os
import pty
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
RESTORE_SH = REPO_ROOT / "scripts" / "restore.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"

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
    manifest.write_text(f'{{"timestamp":"{TS}","schema_version":102,"archives":[]}}')
    result = _run_bash(
        "source scripts/backup.sh --functions-only;"
        f' ENC_KEYFILE="{key}"; sign_manifest "{manifest}"'
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"manifest_{TS}.json.hmac").exists()
    return tmp_path


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
    assert 'mv -f "$tmp" "${manifest}.hmac"' in backup_src
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
    assert backup_src.count('-name "manifest_*.json.hmac"') == 1
    assert backup_src.count('-name "manifest_${ts}.json.hmac"') == 1


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


# --- Signing round-trip -------------------------------------------------------------


def test_a_backup_signature_verifies_in_the_restore_script(signed_set: Path) -> None:
    assert (signed_set / f"manifest_{TS}.json.hmac").read_text().strip() != ""
    assert _verify(signed_set) is True


def test_a_tampered_manifest_fails_verification(signed_set: Path) -> None:
    manifest = signed_set / f"manifest_{TS}.json"
    manifest.write_text(manifest.read_text().replace("102", "103"))
    assert _verify(signed_set) is False


def test_a_wrong_backup_key_fails_verification(signed_set: Path) -> None:
    (signed_set / "other_key").write_text("a-different-key\n")
    assert _verify(signed_set, key_name="other_key") is False


def test_the_signature_is_a_bare_sha256_hex_digest(signed_set: Path) -> None:
    digest = (signed_set / f"manifest_{TS}.json.hmac").read_text().strip()
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


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


def test_break_glass_refuses_on_the_environment_variable_alone(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(
        signed_set,
        source="inbox",
        marker=False,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed="\n",
    )
    assert "PROCEED" not in out
    assert "no authenticated manifest" in out


def test_break_glass_refuses_on_the_typed_phrase_alone(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(signed_set, source="inbox", marker=False, typed=f"{BREAK_GLASS_PHRASE}\n")
    assert "PROCEED" not in out
    assert "to restore it anyway" not in out, "the prompt must not appear without the override"


def test_break_glass_proceeds_with_a_warning_when_both_are_supplied(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(
        signed_set,
        source="inbox",
        marker=False,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed=f"{BREAK_GLASS_PHRASE}\n",
    )
    assert "PROCEED" in out
    assert "WITHOUT manifest authentication" in out


def test_break_glass_never_excuses_a_failed_verification(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").write_text("0" * 64 + "\n")
    out = _gate(
        signed_set,
        source="inbox",
        marker=True,
        env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"},
        typed=f"{BREAK_GLASS_PHRASE}\n",
    )
    assert "PROCEED" not in out
    assert "may have been tampered with" in out


def test_break_glass_is_unreachable_without_an_interactive_terminal(signed_set: Path) -> None:
    (signed_set / f"manifest_{TS}.json.hmac").unlink()
    out = _gate(signed_set, source="inbox", marker=False, env={"JARVIS_RESTORE_ALLOW_LEGACY": "1"})
    assert "PROCEED" not in out
    assert "no authenticated manifest" in out
