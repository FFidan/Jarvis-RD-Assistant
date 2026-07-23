"""Operator-level tests for the fail-fast config-key rotation workflow."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "rotate-config-key.sh"
LIFECYCLE_HELPER = REPO_ROOT / "scripts" / "backup-lifecycle.sh"
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup.sh"
PRUNE_SCRIPT = REPO_ROOT / "scripts" / "prune.sh"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "DEPLOYMENT.md"


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "install"
    scripts = root / "scripts"
    secrets = root / "secrets"
    bin_dir = tmp_path / "bin"
    scripts.mkdir(parents=True)
    secrets.mkdir()
    bin_dir.mkdir()

    old_key = "b2xkLWtleS1mb3Itd29ya2Zsb3ctdGVzdC0xMjM0NTY="
    (root / ".env").write_text(
        f"JARVIS_CONFIG_KEY={old_key}\n"
        "ENVIRONMENT=development\n"
        "COMPOSE_PROJECT_NAME=jarvis-rotation-test\n"
        "COMPOSE_FILE=docker-compose.yml\n",
        encoding="utf-8",
    )
    (root / "docker-compose.yml").write_text(
        "services:\n  postgres-backup:\n    image: postgres:16.8\n",
        encoding="utf-8",
    )
    (root / "versions.env").write_text(
        "JARVIS_VERSION=1.2.0-test\n",
        encoding="utf-8",
    )
    (secrets / "jarvis_config_key.txt").write_text(old_key, encoding="utf-8")
    (scripts / "rotate_config_key.py").write_text("# mounted test tool\n", encoding="utf-8")
    (scripts / "backup-lifecycle.sh").symlink_to(LIFECYCLE_HELPER)
    (scripts / "setup_lib.sh").symlink_to(REPO_ROOT / "scripts/setup_lib.sh")
    (root / "backups").mkdir()
    (root / "trigger").mkdir()

    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu
printf 'args=%s|COMPOSE_FILE=%s|COMPOSE_PROJECT_NAME=%s|COMPOSE_PROFILES=%s\n' \
  "$*" "${COMPOSE_FILE-<unset>}" "${COMPOSE_PROJECT_NAME-<unset>}" \
  "${COMPOSE_PROFILES-<unset>}" >> "$DOCKER_LOG"

args=("$@")
key_role=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    *jarvis_config_key_next.txt:/run/rotation/old:ro) key_role=new ;;
    *jarvis_config_key.txt:/run/rotation/old:ro) key_role=old ;;
  esac
  if [ "${args[$i]}" = /tmp/backup-lifecycle.sh ]; then
    helper_args=("${args[@]:$((i + 1))}")
    if [ "${helper_args[0]:-}" = wait-rotation ] \
       && [ -n "${ROTATION_TEST_WAIT_FAIL_ONCE_FILE:-}" ] \
       && [ ! -e "$ROTATION_TEST_WAIT_FAIL_ONCE_FILE" ]; then
      : > "$ROTATION_TEST_WAIT_FAIL_ONCE_FILE"
      exit 75
    fi
    if [ "${helper_args[0]:-}" = hold-rotation ]; then
      JARVIS_BACKUP_TRIGGER_DIR="$ROTATION_TEST_TRIGGER_DIR" \
        JARVIS_BACKUP_DIR="$ROTATION_TEST_BACKUP_DIR" \
        bash "$ROTATION_LIFECYCLE_HELPER" "${helper_args[@]}" >/dev/null 2>&1 &
      printf 'rotation-helper\n'
      exit 0
    fi
    JARVIS_BACKUP_TRIGGER_DIR="$ROTATION_TEST_TRIGGER_DIR" \
      JARVIS_BACKUP_DIR="$ROTATION_TEST_BACKUP_DIR" \
      bash "$ROTATION_LIFECYCLE_HELPER" "${helper_args[@]}"
    exit $?
  fi
done

case " $* " in
  *" run "*" --probe-state "*)
    if [ -n "${ROTATION_TEST_PROBE_OUTPUT:-}" ]; then
      printf '%s\n' "$ROTATION_TEST_PROBE_OUTPUT"
      exit 0
    fi
    db_state="$(cat "$ROTATION_TEST_DB_STATE_FILE")"
    case "$db_state" in
      old|new|empty|ambiguous)
        rows=1; [ "$db_state" = empty ] && rows=0
        printf 'JARVIS_ROTATION_STATE=%s ROWS=%s\n' "$db_state" "$rows"
        ;;
      *) exit 43 ;;
    esac
    ;;
  *" run "*" --apply "*)
    if [ "${ROTATION_EXPECT_GUARD:-0}" = 1 ]; then
      [ -s "$ROTATION_TEST_BACKUP_DIR/.lifecycle/rotation.guard" ] || exit 70
    fi
    if [ "${ROTATION_TEST_APPLY_COMMIT_NONZERO:-0}" = 1 ]; then
      printf '%s\n' "${ROTATION_TEST_COMMIT_STATE:-new}" > "$ROTATION_TEST_DB_STATE_FILE"
      exit 41
    fi
    [ "${ROTATION_TEST_APPLY_FAIL:-0}" = 1 ] && exit 41
    printf 'new\n' > "$ROTATION_TEST_DB_STATE_FILE"
    ;;
  *" run "*)
    if [ -n "$key_role" ]; then
      db_state="$(cat "$ROTATION_TEST_DB_STATE_FILE")"
      case "${db_state}:${key_role}" in
        old:old|new:new|empty:old|empty:new|ambiguous:old|ambiguous:new) ;;
        *) exit 42 ;;
      esac
      if [ "$key_role" = old ] && [ -n "${ROTATION_TEST_PAUSE_DIR:-}" ]; then
        : > "$ROTATION_TEST_PAUSE_DIR/entered"
        while [ ! -e "$ROTATION_TEST_PAUSE_DIR/release" ]; do sleep 0.02; done
      fi
    fi
    ;;
  *" ps -q postgres "|*" ps -q paper_ingestion "|\
  *" ps -q learning_engine "|*" ps -q postgres-backup ")
    printf 'container-id\n'
    ;;
  *com.docker.compose.project.working_dir*)
    printf '%s|%s|%s/docker-compose.yml\n' \
      "${ROTATION_TEST_LABEL_PROJECT:-jarvis-rotation-test}" \
      "$ROTATION_TEST_ROOT" "$ROTATION_TEST_ROOT"
    ;;
  *" inspect --format {{.State.Health.Status}} container-id "*)
    if [ "${ROTATION_TEST_HEALTH_FAIL:-0}" = 1 ]; then
      printf 'unhealthy\n'
    else
      printf 'healthy\n'
    fi
    ;;
  *" inspect --format {{.State.Status}} container-id "*)
    printf 'running\n'
    ;;
  *" stop paper_ingestion learning_engine"*)
    if [ -n "${ROTATION_TEST_STOP_PAUSE_DIR:-}" ]; then
      : > "$ROTATION_TEST_STOP_PAUSE_DIR/after-stop"
      kill -STOP "$PPID"
    fi
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    (root / "db-state").write_text("old\n", encoding="utf-8")
    return root, bin_dir, old_key


def _rotation_env(
    root: Path,
    bin_dir: Path,
    *,
    apply_fails: bool = False,
    health_fails: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "JARVIS_ROTATION_ROOT": str(root),
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "DOCKER_LOG": str(root / "docker.log"),
            "ROTATION_TEST_APPLY_FAIL": "1" if apply_fails else "0",
            "ROTATION_TEST_HEALTH_FAIL": "1" if health_fails else "0",
            "ROTATION_TEST_TRIGGER_DIR": str(root / "trigger"),
            "ROTATION_TEST_BACKUP_DIR": str(root / "backups"),
            "ROTATION_LIFECYCLE_HELPER": str(LIFECYCLE_HELPER),
            "ROTATION_TEST_ROOT": str(root),
            "ROTATION_TEST_DB_STATE_FILE": str(root / "db-state"),
            "JARVIS_ROTATION_HEALTH_ATTEMPTS": "1",
            "JARVIS_ROTATION_HEALTH_INTERVAL": "0",
            "JARVIS_CLI_CONFIG_DIR": str(root / "cli-state"),
        }
    )
    return env


def _lifecycle_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["JARVIS_BACKUP_TRIGGER_DIR"] = str(root / "trigger")
    env["JARVIS_BACKUP_DIR"] = str(root / "backups")
    return env


def _reserve_rotation_guard(root: Path, guard_id: str) -> None:
    result = subprocess.run(
        ["bash", str(LIFECYCLE_HELPER), "reserve-rotation", guard_id],
        env=_lifecycle_env(root),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run(
    root: Path, bin_dir: Path, *, apply_fails: bool = False
) -> subprocess.CompletedProcess[str]:
    env = _rotation_env(root, bin_dir, apply_fails=apply_fails)
    return subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        capture_output=True,
        env=env,
    )


def test_rotation_promotes_both_stores_without_putting_keys_in_docker_args(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)

    result = _run(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    new_key = (root / "secrets/jarvis_config_key.txt").read_text(encoding="utf-8")
    assert new_key != old_key
    assert f"JARVIS_CONFIG_KEY={new_key}\n" in (root / ".env").read_text(encoding="utf-8")
    docker_log = (root / "docker.log").read_text(encoding="utf-8")
    assert old_key not in docker_log
    assert new_key not in docker_log
    assert "--apply" in docker_log
    assert "--force-recreate paper_ingestion learning_engine" in docker_log
    assert not (root / "secrets/jarvis_config_key_next.txt").exists()
    assert not (root / "secrets/jarvis_config_key_previous.txt").exists()
    assert not (root / ".env.pre-config-key-rotation.bak").exists()


def test_rotation_pins_compose_target_and_scrubs_ambient_selectors(tmp_path: Path) -> None:
    root, bin_dir, _old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir)
    env.update(
        {
            "COMPOSE_FILE": "/tmp/foreign-compose.yml",
            "COMPOSE_PROJECT_NAME": "foreign-project",
            "COMPOSE_PROFILES": "foreign-profile",
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (root / "docker.log").read_text(encoding="utf-8").splitlines()
    expected_prefix = (
        f"args=compose --project-directory {root} --env-file {root / '.env'} "
        f"-p jarvis-rotation-test -f {root / 'docker-compose.yml'} "
    )
    managed_compose_calls = [
        line for line in calls if line.startswith("args=compose --project-directory ")
    ]
    assert managed_compose_calls
    assert all(line.startswith(expected_prefix) for line in managed_compose_calls)
    assert all(
        "COMPOSE_FILE=<unset>|COMPOSE_PROJECT_NAME=<unset>|COMPOSE_PROFILES=<unset>" in line
        for line in managed_compose_calls
    )
    assert all("foreign-" not in line for line in managed_compose_calls)


def test_rotation_refuses_compose_project_owned_by_another_install(tmp_path: Path) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir)
    env["ROTATION_TEST_LABEL_PROJECT"] = "different-install"

    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert "ownership" in result.stderr.lower()
    assert (root / "secrets/jarvis_config_key.txt").read_text(encoding="utf-8") == old_key
    assert " --apply" not in (root / "docker.log").read_text(encoding="utf-8")


def test_rotation_waits_for_backup_mutex_and_marks_apply_as_maintenance(
    tmp_path: Path,
) -> None:
    root, bin_dir, _old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir)
    env["ROTATION_EXPECT_GUARD"] = "1"
    lock_path = root / "backups" / ".lifecycle" / "backup.lock"
    lock_path.parent.mkdir()
    lock_path.touch()

    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        process = subprocess.Popen(
            ["bash", str(SCRIPT), "--yes"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        time.sleep(0.4)
        log = (root / "docker.log").read_text(encoding="utf-8")
        assert " --apply" not in log
        fcntl.flock(lock_file, fcntl.LOCK_UN)

    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stdout + stderr
    assert " --apply" in (root / "docker.log").read_text(encoding="utf-8")
    assert not (root / "backups/.lifecycle/rotation.guard").exists()


def test_rotation_waits_past_the_old_ten_second_readiness_budget(
    tmp_path: Path,
) -> None:
    root, bin_dir, _old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir)
    env["ROTATION_EXPECT_GUARD"] = "1"
    env["ROTATION_TEST_WAIT_FAIL_ONCE_FILE"] = str(root / "wait-helper-failed-once")
    private_lock_path = root / "backups/.lifecycle/backup.lock"
    private_lock_path.parent.mkdir()
    legacy_lock_path = root / "trigger/.backup.lock"
    legacy_lock_path.touch()
    private_lock_path.touch()
    legacy_lock = legacy_lock_path.open("a", encoding="utf-8")
    private_lock = private_lock_path.open("a", encoding="utf-8")
    fcntl.flock(legacy_lock, fcntl.LOCK_EX)
    fcntl.flock(private_lock, fcntl.LOCK_EX)
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        time.sleep(10.5)
        assert process.poll() is None, (
            "the wrapper returned while its detached guard could still activate later"
        )
        fcntl.flock(legacy_lock, fcntl.LOCK_UN)
        fcntl.flock(private_lock, fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr
        docker_log = (root / "docker.log").read_text(encoding="utf-8")
        assert " --apply" in docker_log
        assert docker_log.count(" wait-rotation ") == 2
        assert docker_log.count(" rotation-reservation-status ") < 20
        assert not (root / "backups/.lifecycle/rotation.guard").exists()
    finally:
        fcntl.flock(legacy_lock, fcntl.LOCK_UN)
        fcntl.flock(private_lock, fcntl.LOCK_UN)
        legacy_lock.close()
        private_lock.close()
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=3)
        _release_test_rotation_guard(root)


def test_rotation_timeout_returns_only_after_helper_cannot_activate(
    tmp_path: Path,
) -> None:
    root, bin_dir, _old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir)
    env["JARVIS_ROTATION_GUARD_TIMEOUT"] = "1"
    lock_path = root / "backups/.lifecycle/backup.lock"
    lock_path.parent.mkdir()
    lock_path.touch()
    lock_file = lock_path.open("a", encoding="utf-8")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--yes"],
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )
        assert result.returncode != 0
        assert "timed out before activation" in result.stderr
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

    time.sleep(0.3)
    assert not (root / "backups/.lifecycle/rotation.guard").exists()
    guard_id = (
        (root / "secrets/jarvis_config_key_rotation_state.txt")
        .read_text(encoding="utf-8")
        .split("guard_id=", 1)[1]
        .splitlines()[0]
    )
    owner = subprocess.run(
        [
            "bash",
            str(LIFECYCLE_HELPER),
            "rotation-reservation-status",
            guard_id,
        ],
        env=_lifecycle_env(root),
        check=False,
        capture_output=True,
    )
    assert owner.returncode != 0


def test_lifecycle_mutexes_ignore_trigger_volume_symlinks(tmp_path: Path) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    victim = root / "backups/victim"
    victim.write_text("must-not-change\n", encoding="utf-8")
    legacy_lock = root / "trigger/.config-key-rotation-reservation.lock"
    legacy_lock.symlink_to(victim)

    result = subprocess.run(
        [
            "bash",
            str(LIFECYCLE_HELPER),
            "reserve-rotation",
            "0123456789abcdef0123456789abcdef",
        ],
        text=True,
        capture_output=True,
        env=_lifecycle_env(root),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "launch"
    assert victim.read_text(encoding="utf-8") == "must-not-change\n"


def test_backup_update_prune_and_rotation_share_private_lock_paths() -> None:
    helper = LIFECYCLE_HELPER.read_text(encoding="utf-8")
    backup = BACKUP_SCRIPT.read_text(encoding="utf-8")
    prune = PRUNE_SCRIPT.read_text(encoding="utf-8")

    for text in (helper, backup, prune):
        assert 'LOCK_DIR="${BACKUP_DIR}/.lifecycle"' in text
    assert 'ROTATION_LOCK="${LOCK_DIR}/backup.lock"' in helper
    assert 'BACKUP_LOCK="${LOCK_DIR}/backup.lock"' in backup
    assert 'UPDATE_LOCK="${LOCK_DIR}/update.lock"' in helper
    assert 'UPDATE_LOCK="${LOCK_DIR}/update.lock"' in backup
    assert 'UPDATE_LOCK="${LOCK_DIR}/update.lock"' in prune
    for unsafe_open in (
        'exec 9>"${TRIGGER_DIR}/.backup.lock"',
        'exec 7>"${TRIGGER_DIR}/.backup-lifecycle.lock"',
        'exec 7>"$ROTATION_RESERVATION_LOCK"',
    ):
        assert unsafe_open not in "\n".join((helper, backup, prune))


def test_pending_update_guard_has_one_durable_identity_and_is_adoptable(
    tmp_path: Path,
) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    helper_env = _lifecycle_env(root)
    guard_id = "abcdef0123456789abcdef0123456789"
    update_lock_path = root / "backups/.lifecycle/update.lock"
    update_lock_path.parent.mkdir()
    update_lock_path.touch()
    update_lock = update_lock_path.open("a", encoding="utf-8")
    fcntl.flock(update_lock, fcntl.LOCK_EX)

    reserve = subprocess.run(
        ["bash", str(LIFECYCLE_HELPER), "reserve-update", guard_id],
        text=True,
        capture_output=True,
        env=helper_env,
    )
    assert reserve.returncode == 0, reserve.stdout + reserve.stderr
    assert reserve.stdout.strip() == "launch"
    holder = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-update", guard_id, "30"],
        env=helper_env,
    )
    try:
        for _ in range(200):
            owner = subprocess.run(
                [
                    "bash",
                    str(LIFECYCLE_HELPER),
                    "update-reservation-status",
                    guard_id,
                ],
                env=helper_env,
                check=False,
                capture_output=True,
            )
            if owner.returncode == 0:
                break
            time.sleep(0.02)
        assert owner.returncode == 0

        adopted = subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), "reserve-update", guard_id],
            text=True,
            capture_output=True,
            env=helper_env,
        )
        assert adopted.returncode == 0, adopted.stdout + adopted.stderr
        assert adopted.stdout.strip() == "adopt"
        current = subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), "current-update-reservation"],
            text=True,
            capture_output=True,
            env=helper_env,
        )
        assert current.returncode == 0
        assert current.stdout.strip() == guard_id

        fcntl.flock(update_lock, fcntl.LOCK_UN)
        activated = subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), "wait-update", guard_id, "100", "0.1"],
            env=helper_env,
            check=False,
            timeout=5,
        )
        assert activated.returncode == 0
        released = subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), "release-update", guard_id],
            env=helper_env,
            check=False,
        )
        assert released.returncode == 0
        holder.wait(timeout=3)
        assert not (root / "backups/.lifecycle/update.reservation").exists()
    finally:
        fcntl.flock(update_lock, fcntl.LOCK_UN)
        update_lock.close()
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=3)


def test_update_wait_timeout_cannot_activate_a_guard_later(tmp_path: Path) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    helper_env = _lifecycle_env(root)
    guard_id = "abcdef0123456789abcdef0123456789"
    lock_path = root / "backups/.lifecycle/update.lock"
    lock_path.parent.mkdir()
    lock_path.touch()
    lock_file = lock_path.open("a", encoding="utf-8")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    reserve = subprocess.run(
        ["bash", str(LIFECYCLE_HELPER), "reserve-update", guard_id],
        env=helper_env,
        check=False,
        capture_output=True,
    )
    assert reserve.returncode == 0
    holder = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-update", guard_id, "1"],
        env=helper_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            owner = subprocess.run(
                [
                    "bash",
                    str(LIFECYCLE_HELPER),
                    "update-reservation-status",
                    guard_id,
                ],
                env=helper_env,
                check=False,
                capture_output=True,
            )
            if owner.returncode == 0:
                break
            time.sleep(0.01)
        assert owner.returncode == 0
        observer = subprocess.run(
            [
                "bash",
                str(LIFECYCLE_HELPER),
                "wait-update",
                guard_id,
                "20",
                "0.05",
            ],
            text=True,
            capture_output=True,
            env=helper_env,
            timeout=4,
        )
        assert observer.returncode != 0
        assert "stopped before guard activation" in observer.stderr
        holder_stdout, holder_stderr = holder.communicate(timeout=2)
        assert holder.returncode != 0, holder_stdout + holder_stderr
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        if holder.poll() is None:
            holder.terminate()
            holder.communicate(timeout=3)

    time.sleep(0.3)
    assert not (root / "backups/.lifecycle/update.guard").exists()
    owner = subprocess.run(
        [
            "bash",
            str(LIFECYCLE_HELPER),
            "update-reservation-status",
            guard_id,
        ],
        env=helper_env,
        check=False,
        capture_output=True,
    )
    assert owner.returncode != 0


@pytest.mark.parametrize(
    ("kind", "reserve_command", "wait_command", "hold_command", "release_command"),
    [
        (
            "update",
            "reserve-update",
            "wait-update",
            "hold-update",
            ("release-update",),
        ),
        (
            "rotation",
            "reserve-rotation",
            "wait-rotation",
            "hold-rotation",
            ("release-rotation", "clear"),
        ),
    ],
)
def test_lifecycle_observer_never_becomes_the_lock_owner(
    tmp_path: Path,
    kind: str,
    reserve_command: str,
    wait_command: str,
    hold_command: str,
    release_command: tuple[str, ...],
) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    helper_env = _lifecycle_env(root)
    guard_id = "abcdef0123456789abcdef0123456789"
    reserve = subprocess.run(
        ["bash", str(LIFECYCLE_HELPER), reserve_command, guard_id],
        text=True,
        capture_output=True,
        env=helper_env,
    )
    assert reserve.returncode == 0, reserve.stdout + reserve.stderr

    observer = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), wait_command, guard_id, "20", "0.5"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=helper_env,
    )
    holder: subprocess.Popen[str] | None = None
    try:
        time.sleep(0.15)
        lock_dir = root / "backups/.lifecycle"
        operation_lock = lock_dir / ("update.lock" if kind == "update" else "backup.lock")
        reservation_lock = lock_dir / f"{kind}-reservation.lock"
        for path in (operation_lock, reservation_lock):
            probe = path.open("a", encoding="utf-8")
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            finally:
                probe.close()

        holder = subprocess.Popen(
            ["bash", str(LIFECYCLE_HELPER), hold_command, guard_id, "5"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=helper_env,
        )
        observer_stdout, observer_stderr = observer.communicate(timeout=4)
        assert observer.returncode == 0, observer_stdout + observer_stderr
        released = subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), *release_command, guard_id]
            if kind == "update"
            else [
                "bash",
                str(LIFECYCLE_HELPER),
                *release_command[:1],
                guard_id,
                *release_command[1:],
            ],
            text=True,
            capture_output=True,
            env=helper_env,
        )
        assert released.returncode == 0, released.stdout + released.stderr
        holder_stdout, holder_stderr = holder.communicate(timeout=3)
        assert holder.returncode == 0, holder_stdout + holder_stderr
    finally:
        if observer.poll() is None:
            observer.terminate()
            observer.communicate(timeout=3)
        if holder is not None and holder.poll() is None:
            holder.terminate()
            holder.communicate(timeout=3)


@pytest.mark.parametrize("kind", ["update", "rotation"])
def test_duplicate_lifecycle_holder_cannot_reactivate_after_release(
    tmp_path: Path,
    kind: str,
) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    helper_env = _lifecycle_env(root)
    guard_id = "abcdef0123456789abcdef0123456789"
    reserve_command = f"reserve-{kind}"
    hold_command = f"hold-{kind}"
    guard_name = "update.guard" if kind == "update" else "rotation.guard"
    reservation_name = "update.reservation" if kind == "update" else "rotation.reservation"
    reserve = subprocess.run(
        ["bash", str(LIFECYCLE_HELPER), reserve_command, guard_id],
        env=helper_env,
        check=False,
        capture_output=True,
    )
    assert reserve.returncode == 0
    first = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), hold_command, guard_id, "5"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=helper_env,
    )
    second: subprocess.Popen[str] | None = None
    try:
        guard_path = root / "backups/.lifecycle" / guard_name
        for _ in range(200):
            if guard_path.is_file():
                break
            time.sleep(0.01)
        assert guard_path.read_text(encoding="utf-8").strip() == guard_id
        second = subprocess.Popen(
            ["bash", str(LIFECYCLE_HELPER), hold_command, guard_id, "5"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=helper_env,
        )
        time.sleep(0.15)
        if kind == "update":
            release_command = ["release-update", guard_id]
        else:
            release_command = ["release-rotation", guard_id, "clear"]
        released = subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), *release_command],
            text=True,
            capture_output=True,
            env=helper_env,
        )
        assert released.returncode == 0, released.stdout + released.stderr
        first_stdout, first_stderr = first.communicate(timeout=2)
        assert first.returncode == 0, first_stdout + first_stderr
        second_stdout, second_stderr = second.communicate(timeout=1)
        assert second.returncode != 0, second_stdout + second_stderr
        time.sleep(0.2)
        assert not guard_path.exists()
        assert not (root / "backups/.lifecycle" / reservation_name).exists()
    finally:
        if first.poll() is None:
            first.terminate()
            first.communicate(timeout=3)
        if second is not None and second.poll() is None:
            second.terminate()
            second.communicate(timeout=3)


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [("update", "rotation"), ("rotation", "update")],
)
def test_lifecycle_operations_refuse_cross_operation_overlap_and_retry(
    tmp_path: Path,
    first_kind: str,
    second_kind: str,
) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    helper_env = _lifecycle_env(root)
    ids = {
        "update": "abcdef0123456789abcdef0123456789",
        "rotation": "0123456789abcdef0123456789abcdef",
    }
    guard_paths = {
        "update": root / "backups/.lifecycle/update.guard",
        "rotation": root / "backups/.lifecycle/rotation.guard",
    }

    def reserve(kind: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), f"reserve-{kind}", ids[kind]],
            text=True,
            capture_output=True,
            env=helper_env,
        )

    def start(kind: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                "bash",
                str(LIFECYCLE_HELPER),
                f"hold-{kind}",
                ids[kind],
                "5",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=helper_env,
        )

    def release(kind: str) -> subprocess.CompletedProcess[str]:
        command = ["release-update", ids[kind]]
        if kind == "rotation":
            command = ["release-rotation", ids[kind], "clear"]
        return subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), *command],
            text=True,
            capture_output=True,
            env=helper_env,
        )

    def wait_until_active(kind: str) -> None:
        for _ in range(200):
            if guard_paths[kind].is_file():
                return
            time.sleep(0.01)
        pytest.fail(f"{kind} lifecycle guard did not activate")

    first_reservation = reserve(first_kind)
    assert first_reservation.returncode == 0
    first = start(first_kind)
    second: subprocess.Popen[str] | None = None
    retry: subprocess.Popen[str] | None = None
    try:
        wait_until_active(first_kind)
        second_reservation = reserve(second_kind)
        assert second_reservation.returncode != 0
        assert "another lifecycle operation is active" in second_reservation.stderr
        assert not guard_paths[second_kind].exists()
        second_reservation_path = root / "backups/.lifecycle" / f"{second_kind}.reservation"
        if second_kind == "rotation":
            second_reservation_path = root / "backups/.lifecycle/rotation.reservation"
        assert not second_reservation_path.exists()
        time.sleep(0.3)
        assert not guard_paths[second_kind].exists()

        first_release = release(first_kind)
        assert first_release.returncode == 0, first_release.stdout + first_release.stderr
        first_stdout, first_stderr = first.communicate(timeout=3)
        assert first.returncode == 0, first_stdout + first_stderr

        retry_reservation = reserve(second_kind)
        assert retry_reservation.returncode == 0
        assert retry_reservation.stdout.strip() == "launch"
        retry = start(second_kind)
        wait_until_active(second_kind)
        retry_release = release(second_kind)
        assert retry_release.returncode == 0, retry_release.stdout + retry_release.stderr
        retry_stdout, retry_stderr = retry.communicate(timeout=3)
        assert retry.returncode == 0, retry_stdout + retry_stderr
        assert not guard_paths[second_kind].exists()
    finally:
        for kind, process in (
            (first_kind, first),
            (second_kind, second),
            (second_kind, retry),
        ):
            if process is not None and process.poll() is None:
                release(kind)
                process.terminate()
                process.communicate(timeout=3)


def test_unsafe_legacy_backup_lock_fails_closed_without_touching_target(
    tmp_path: Path,
) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    victim = root / "backups/legacy-victim"
    victim.write_text("must-not-change\n", encoding="utf-8")
    (root / "trigger/.backup.lock").symlink_to(victim)
    _reserve_rotation_guard(root, "0123456789abcdef0123456789abcdef")

    result = subprocess.run(
        [
            "bash",
            str(LIFECYCLE_HELPER),
            "hold-rotation",
            "0123456789abcdef0123456789abcdef",
            "2",
        ],
        text=True,
        capture_output=True,
        env=_lifecycle_env(root),
        timeout=5,
    )

    assert result.returncode != 0
    assert "unsafe legacy backup lock" in result.stderr.lower()
    assert victim.read_text(encoding="utf-8") == "must-not-change\n"
    assert not (root / "backups/.lifecycle/rotation.guard").exists()


def test_replacing_legacy_backup_lock_cannot_split_rotation_mutex(
    tmp_path: Path,
) -> None:
    root, _bin_dir, _old_key = _fixture(tmp_path)
    guard_id = "0123456789abcdef0123456789abcdef"
    helper_env = _lifecycle_env(root)
    _reserve_rotation_guard(root, guard_id)
    helper = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-rotation", guard_id, "30"],
        env=helper_env,
    )
    try:
        sentinel = root / "backups/.lifecycle/rotation.guard"
        for _ in range(200):
            if sentinel.is_file():
                break
            time.sleep(0.02)
        assert sentinel.read_text(encoding="utf-8").strip() == guard_id

        legacy_lock = root / "trigger/.backup.lock"
        legacy_lock.unlink(missing_ok=True)
        legacy_lock.touch()
        status = subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), "rotation-status", guard_id],
            env=helper_env,
            check=False,
        )
        assert status.returncode == 0, "trigger-path replacement split the mutex inode"
    finally:
        subprocess.run(
            ["bash", str(LIFECYCLE_HELPER), "release-rotation", guard_id, "clear"],
            env=helper_env,
            check=False,
            capture_output=True,
        )
        if helper.poll() is None:
            helper.terminate()
        helper.wait(timeout=3)


def test_post_mutation_health_failure_keeps_backups_blocked(tmp_path: Path) -> None:
    root, bin_dir, _old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir, health_fails=True)
    env["ROTATION_EXPECT_GUARD"] = "1"

    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert (root / "backups/.lifecycle/rotation.guard").is_file()
    assert "Rotation needs attention" in result.stderr


def test_failed_apply_keeps_old_key_and_restarts_old_services(tmp_path: Path) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)

    result = _run(root, bin_dir, apply_fails=True)

    assert result.returncode != 0
    assert (root / "secrets/jarvis_config_key.txt").read_text(encoding="utf-8") == old_key
    assert f"JARVIS_CONFIG_KEY={old_key}\n" in (root / ".env").read_text(encoding="utf-8")
    assert (root / "secrets/jarvis_config_key_next.txt").is_file()
    assert (root / "secrets/jarvis_config_key_previous.txt").is_file()
    assert (root / ".env.pre-config-key-rotation.bak").is_file()
    docker_log = (root / "docker.log").read_text(encoding="utf-8")
    assert " up -d paper_ingestion learning_engine postgres-backup" in docker_log

    retry = _run(root, bin_dir, apply_fails=True)

    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert (root / "secrets/jarvis_config_key.txt").read_text() == old_key
    assert not (root / "secrets/jarvis_config_key_rotation_state.txt").exists()
    assert not (root / "backups/.lifecycle/rotation.guard").exists()


def test_committed_apply_with_lost_client_result_is_reconciled_before_restart(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir)
    env["ROTATION_TEST_APPLY_COMMIT_NONZERO"] = "1"

    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "secrets/jarvis_config_key.txt").read_text() != old_key
    assert not (root / "backups/.lifecycle/rotation.guard").exists()
    docker_calls = (root / "docker.log").read_text(encoding="utf-8").splitlines()
    assert any(
        " up -d --force-recreate paper_ingestion learning_engine postgres-backup" in call
        for call in docker_calls
    )
    assert not any(
        " up -d paper_ingestion learning_engine postgres-backup" in call for call in docker_calls
    )


def test_lost_apply_result_with_zero_encrypted_rows_promotes_without_operator_wedge(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    (root / "db-state").write_text("empty\n", encoding="utf-8")
    env = _rotation_env(root, bin_dir)
    env["ROTATION_TEST_APPLY_COMMIT_NONZERO"] = "1"
    env["ROTATION_TEST_COMMIT_STATE"] = "empty"

    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "secrets/jarvis_config_key.txt").read_text() != old_key
    assert not (root / "backups/.lifecycle/rotation.guard").exists()


def _stage_unknown_rotation(root: Path, old_key: str, db_state: str) -> str:
    next_key = "bmV3LWtleS1mb3Itd29ya2Zsb3ctdGVzdC0xMjM0NTY="
    (root / "secrets/jarvis_config_key_next.txt").write_text(next_key)
    (root / "secrets/jarvis_config_key_previous.txt").write_text(old_key)
    (root / ".env.pre-config-key-rotation.bak").write_text(
        (root / ".env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "secrets/jarvis_config_key_rotation_state.txt").write_text(
        "mutation-unknown\nbackup_service_was_running=1\n",
        encoding="utf-8",
    )
    (root / "db-state").write_text(f"{db_state}\n", encoding="utf-8")
    return next_key


def _stage_finalizing_rotation(
    root: Path,
    old_key: str,
    *,
    missing: tuple[str, ...] = (),
    guard_id: str = "",
) -> str:
    next_key = "bmV3LWtleS1mb3Itd29ya2Zsb3ctdGVzdC0xMjM0NTY="
    env_text = (root / ".env").read_text(encoding="utf-8")
    (root / ".env.pre-config-key-rotation.bak").write_text(env_text, encoding="utf-8")
    (root / ".env").write_text(
        env_text.replace(old_key, next_key),
        encoding="utf-8",
    )
    (root / "secrets/jarvis_config_key.txt").write_text(next_key)
    (root / "secrets/jarvis_config_key_next.txt").write_text(next_key)
    (root / "secrets/jarvis_config_key_previous.txt").write_text(old_key)
    (root / "secrets/jarvis_config_key_rotation_state.txt").write_text(
        f"finalizing\nbackup_service_was_running=1\nguard_id={guard_id}\n",
        encoding="utf-8",
    )
    (root / "db-state").write_text("new\n", encoding="utf-8")
    paths = {
        "next": root / "secrets/jarvis_config_key_next.txt",
        "previous": root / "secrets/jarvis_config_key_previous.txt",
        "env_backup": root / ".env.pre-config-key-rotation.bak",
    }
    for name in missing:
        paths[name].unlink()
    return next_key


def _stage_cancelling_rotation(
    root: Path,
    old_key: str,
    *,
    missing: tuple[str, ...] = (),
) -> None:
    next_key = "bmV3LWtleS1mb3Itd29ya2Zsb3ctdGVzdC0xMjM0NTY="
    (root / "secrets/jarvis_config_key_next.txt").write_text(next_key)
    (root / "secrets/jarvis_config_key_previous.txt").write_text(old_key)
    (root / ".env.pre-config-key-rotation.bak").write_text(
        (root / ".env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "secrets/jarvis_config_key_rotation_state.txt").write_text(
        "cancelling\nbackup_service_was_running=1\nguard_id=\ncancel_restart_services=0\n",
        encoding="utf-8",
    )
    paths = {
        "next": root / "secrets/jarvis_config_key_next.txt",
        "previous": root / "secrets/jarvis_config_key_previous.txt",
        "env_backup": root / ".env.pre-config-key-rotation.bak",
    }
    for name in missing:
        paths[name].unlink()


def _release_test_rotation_guard(root: Path) -> None:
    helper_env = _lifecycle_env(root)
    sentinel = root / "backups/.lifecycle/rotation.guard"
    for _ in range(100):
        if sentinel.is_file():
            guard_id = sentinel.read_text(encoding="utf-8").strip()
            subprocess.run(
                [
                    "bash",
                    str(LIFECYCLE_HELPER),
                    "release-rotation",
                    guard_id,
                    "clear",
                ],
                env=helper_env,
                check=False,
                capture_output=True,
            )
            return
        time.sleep(0.02)


def test_crash_resume_reconciles_a_definitely_committed_unknown_phase(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    next_key = _stage_unknown_rotation(root, old_key, "new")

    result = _run(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "secrets/jarvis_config_key.txt").read_text() == next_key
    assert not (root / "backups/.lifecycle/rotation.guard").exists()


def test_crash_resume_keeps_services_and_backups_stopped_when_state_is_ambiguous(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    _stage_unknown_rotation(root, old_key, "ambiguous")

    result = _run(root, bin_dir)

    assert result.returncode != 0
    assert (root / "backups/.lifecycle/rotation.guard").is_file()
    assert (
        (root / "secrets/jarvis_config_key_rotation_state.txt")
        .read_text()
        .startswith("mutation-unknown\n")
    )
    docker_log = (root / "docker.log").read_text(encoding="utf-8")
    assert " stop paper_ingestion learning_engine postgres-backup" in docker_log
    assert " up -d paper_ingestion learning_engine postgres-backup" not in docker_log


def test_crash_resume_rejects_an_internally_inconsistent_probe_result(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    _stage_unknown_rotation(root, old_key, "old")
    env = _rotation_env(root, bin_dir)
    env["ROTATION_TEST_PROBE_OUTPUT"] = "JARVIS_ROTATION_STATE=old ROWS=0"

    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert (root / "backups/.lifecycle/rotation.guard").is_file()
    assert (root / "secrets/jarvis_config_key.txt").read_text() == old_key
    assert "invalid old-key state" in result.stderr


@pytest.mark.parametrize(
    "missing",
    [
        (),
        ("next",),
        ("previous",),
        ("env_backup",),
        ("next", "previous"),
        ("next", "env_backup"),
        ("previous", "env_backup"),
        ("next", "previous", "env_backup"),
    ],
)
def test_finalizing_rerun_is_idempotent_after_every_partial_cleanup_permutation(
    tmp_path: Path,
    missing: tuple[str, ...],
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    next_key = _stage_finalizing_rotation(root, old_key, missing=missing)

    result = _run(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "secrets/jarvis_config_key.txt").read_text() == next_key
    assert f"JARVIS_CONFIG_KEY={next_key}\n" in (root / ".env").read_text()
    for path in (
        root / "secrets/jarvis_config_key_next.txt",
        root / "secrets/jarvis_config_key_previous.txt",
        root / "secrets/jarvis_config_key_rotation_state.txt",
        root / ".env.pre-config-key-rotation.bak",
    ):
        assert not path.exists()
    assert " --apply" not in (root / "docker.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "missing",
    [
        (),
        ("next",),
        ("previous",),
        ("env_backup",),
        ("next", "previous"),
        ("next", "env_backup"),
        ("previous", "env_backup"),
        ("next", "previous", "env_backup"),
    ],
)
def test_cancelling_rerun_is_idempotent_after_every_partial_cleanup_permutation(
    tmp_path: Path,
    missing: tuple[str, ...],
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    _stage_cancelling_rotation(root, old_key, missing=missing)

    result = _run(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "secrets/jarvis_config_key.txt").read_text() == old_key
    assert f"JARVIS_CONFIG_KEY={old_key}\n" in (root / ".env").read_text()
    for path in (
        root / "secrets/jarvis_config_key_next.txt",
        root / "secrets/jarvis_config_key_previous.txt",
        root / "secrets/jarvis_config_key_rotation_state.txt",
        root / ".env.pre-config-key-rotation.bak",
    ):
        assert not path.exists()
    assert " --apply" not in (root / "docker.log").read_text(encoding="utf-8")


def test_finalizing_rerun_adopts_and_releases_the_pre_crash_rotation_guard(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    env = _rotation_env(root, bin_dir)
    guard_id = "0123456789abcdef0123456789abcdef"
    helper_env = _lifecycle_env(root)
    _reserve_rotation_guard(root, guard_id)
    helper = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-rotation", guard_id, "30"],
        env=helper_env,
    )
    try:
        sentinel = root / "backups/.lifecycle/rotation.guard"
        for _ in range(100):
            if sentinel.exists():
                break
            time.sleep(0.02)
        assert sentinel.read_text().strip() == guard_id
        _stage_finalizing_rotation(root, old_key, guard_id=guard_id)

        result = subprocess.run(
            ["bash", str(SCRIPT), "--yes"],
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Adopting the existing backup maintenance guard" in result.stdout
        assert not sentinel.exists()
        helper.wait(timeout=3)
    finally:
        if helper.poll() is None:
            subprocess.run(
                [
                    "bash",
                    str(LIFECYCLE_HELPER),
                    "release-rotation",
                    guard_id,
                    "clear",
                ],
                env=helper_env,
                check=False,
            )
            helper.terminate()
            helper.wait(timeout=3)


def test_prepared_rerun_adopts_the_guard_left_by_a_crash_before_service_stop(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    guard_id = "abcdef0123456789abcdef0123456789"
    next_key = "bmV3LWtleS1mb3Itd29ya2Zsb3ctdGVzdC0xMjM0NTY="
    (root / "secrets/jarvis_config_key_next.txt").write_text(next_key)
    (root / "secrets/jarvis_config_key_previous.txt").write_text(old_key)
    (root / ".env.pre-config-key-rotation.bak").write_text(
        (root / ".env").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "secrets/jarvis_config_key_rotation_state.txt").write_text(
        f"prepared\nbackup_service_was_running=1\nguard_id={guard_id}\n",
        encoding="utf-8",
    )
    helper_env = _lifecycle_env(root)
    _reserve_rotation_guard(root, guard_id)
    helper = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-rotation", guard_id, "30"],
        env=helper_env,
    )
    try:
        sentinel = root / "backups/.lifecycle/rotation.guard"
        for _ in range(100):
            if sentinel.exists():
                break
            time.sleep(0.02)
        assert sentinel.read_text().strip() == guard_id

        result = _run(root, bin_dir)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Adopting the existing backup maintenance guard" in result.stdout
        assert not sentinel.exists()
        helper.wait(timeout=3)
    finally:
        if helper.poll() is None:
            subprocess.run(
                [
                    "bash",
                    str(LIFECYCLE_HELPER),
                    "release-rotation",
                    guard_id,
                    "clear",
                ],
                env=helper_env,
                check=False,
            )
            helper.terminate()
            helper.wait(timeout=3)


def test_finalizing_rerun_replaces_a_stale_unlocked_rotation_sentinel(
    tmp_path: Path,
) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    stale_id = "fedcba9876543210fedcba9876543210"
    _stage_finalizing_rotation(root, old_key, guard_id=stale_id)
    sentinel = root / "backups/.lifecycle/rotation.guard"
    sentinel.parent.mkdir(exist_ok=True)
    sentinel.write_text(f"{stale_id}\n", encoding="utf-8")

    result = _run(root, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not sentinel.exists()
    assert "hold-rotation" in (root / "docker.log").read_text(encoding="utf-8")


def test_second_rotation_fails_before_it_can_touch_staging_or_docker(
    tmp_path: Path,
) -> None:
    root, bin_dir, _old_key = _fixture(tmp_path)
    pause_dir = root / "pause"
    pause_dir.mkdir()
    first_env = _rotation_env(root, bin_dir)
    first_env["ROTATION_TEST_PAUSE_DIR"] = str(pause_dir)
    first_env["DOCKER_LOG"] = str(root / "docker-first.log")
    first = subprocess.Popen(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=first_env,
    )
    try:
        for _ in range(100):
            if (pause_dir / "entered").exists():
                break
            time.sleep(0.02)
        assert (pause_dir / "entered").exists()
        next_before = (root / "secrets/jarvis_config_key_next.txt").read_bytes()
        state_before = (root / "secrets/jarvis_config_key_rotation_state.txt").read_bytes()
        second_env = _rotation_env(root, bin_dir)
        second_env["DOCKER_LOG"] = str(root / "docker-second.log")

        second = subprocess.run(
            ["bash", str(SCRIPT), "--yes"],
            text=True,
            capture_output=True,
            env=second_env,
            timeout=3,
        )

        assert second.returncode != 0
        assert "lifecycle operation is already running" in second.stderr
        assert not (root / "docker-second.log").exists()
        assert (root / "secrets/jarvis_config_key_next.txt").read_bytes() == next_before
        assert (root / "secrets/jarvis_config_key_rotation_state.txt").read_bytes() == state_before
        (pause_dir / "release").touch()
        stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, stdout + stderr
    finally:
        (pause_dir / "release").touch()
        if first.poll() is None:
            first.terminate()
            first.communicate(timeout=3)


def test_retry_adopts_one_pending_guard_while_an_active_backup_finishes(
    tmp_path: Path,
) -> None:
    root, bin_dir, _old_key = _fixture(tmp_path)
    first_env = _rotation_env(root, bin_dir)
    first_env["DOCKER_LOG"] = str(root / "docker-first.log")
    backup_lock_path = root / "backups/.lifecycle/backup.lock"
    backup_lock_path.parent.mkdir()
    backup_lock_path.touch()
    backup_lock = backup_lock_path.open("w", encoding="utf-8")
    fcntl.flock(backup_lock, fcntl.LOCK_EX)
    first = subprocess.Popen(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=first_env,
    )
    retry: subprocess.Popen[str] | None = None
    backup_released = False
    try:
        state_path = root / "secrets/jarvis_config_key_rotation_state.txt"
        guard_id = ""
        for _ in range(200):
            state = state_path.read_text(encoding="utf-8") if state_path.exists() else ""
            guard_lines = [line for line in state.splitlines() if line.startswith("guard_id=")]
            if guard_lines and guard_lines[0].split("=", 1)[1]:
                guard_id = guard_lines[0].split("=", 1)[1]
                break
            time.sleep(0.02)
        assert len(guard_id) == 32
        reservation = root / "backups/.lifecycle/rotation.reservation"
        for _ in range(200):
            if reservation.is_file():
                break
            time.sleep(0.02)
        assert reservation.read_text(encoding="utf-8").strip() == guard_id
        helper_env = _lifecycle_env(root)
        owner_ready = False
        for _ in range(200):
            owner = subprocess.run(
                [
                    "bash",
                    str(LIFECYCLE_HELPER),
                    "rotation-reservation-status",
                    guard_id,
                ],
                env=helper_env,
                check=False,
                capture_output=True,
            )
            if owner.returncode == 0:
                owner_ready = True
                break
            time.sleep(0.02)
        assert owner_ready
        assert not (root / "backups/.lifecycle/rotation.guard").exists()

        first.kill()
        first.communicate(timeout=3)
        retry_env = _rotation_env(root, bin_dir)
        retry_env["DOCKER_LOG"] = str(root / "docker-retry.log")
        retry = subprocess.Popen(
            ["bash", str(SCRIPT), "--yes"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=retry_env,
        )
        time.sleep(0.3)
        assert retry.poll() is None, retry.communicate(timeout=3)
        assert f"guard_id={guard_id}\n" in state_path.read_text(encoding="utf-8")
        logs = "".join(
            path.read_text(encoding="utf-8")
            for path in (root / "docker-first.log", root / "docker-retry.log")
            if path.exists()
        )
        assert logs.count(" hold-rotation ") == 1

        fcntl.flock(backup_lock, fcntl.LOCK_UN)
        backup_released = True
        stdout, stderr = retry.communicate(timeout=10)
        assert retry.returncode == 0, stdout + stderr
        assert "Adopting the existing backup maintenance guard" in stdout
        final_logs = "".join(
            path.read_text(encoding="utf-8")
            for path in (root / "docker-first.log", root / "docker-retry.log")
            if path.exists()
        )
        assert final_logs.count(" hold-rotation ") == 1
        assert not reservation.exists()
        assert not (root / "backups/.lifecycle/rotation.guard").exists()
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=3)
        if retry is not None and retry.poll() is None:
            retry.kill()
            retry.communicate(timeout=3)
        if not backup_released:
            fcntl.flock(backup_lock, fcntl.LOCK_UN)
        backup_lock.close()
        _release_test_rotation_guard(root)


def test_crash_after_service_stop_resumes_as_safe_cancellation(tmp_path: Path) -> None:
    root, bin_dir, old_key = _fixture(tmp_path)
    pause_dir = root / "stop-pause"
    pause_dir.mkdir()
    first_env = _rotation_env(root, bin_dir)
    first_env["ROTATION_TEST_STOP_PAUSE_DIR"] = str(pause_dir)
    first = subprocess.Popen(
        ["bash", str(SCRIPT), "--yes"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=first_env,
    )
    try:
        for _ in range(200):
            if (pause_dir / "after-stop").exists():
                break
            time.sleep(0.02)
        assert (pause_dir / "after-stop").exists()
        state_path = root / "secrets/jarvis_config_key_rotation_state.txt"
        assert state_path.read_text(encoding="utf-8").startswith("quiescing\n")
        assert " --apply" not in (root / "docker.log").read_text(encoding="utf-8")
        first.kill()
        first.communicate(timeout=3)

        retry_env = _rotation_env(root, bin_dir)
        result = subprocess.run(
            ["bash", str(SCRIPT), "--yes"],
            text=True,
            capture_output=True,
            env=retry_env,
            timeout=10,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Cancelling the interrupted pre-mutation rotation" in result.stdout
        assert (root / "secrets/jarvis_config_key.txt").read_text() == old_key
        assert f"JARVIS_CONFIG_KEY={old_key}\n" in (root / ".env").read_text()
        assert " --apply" not in (root / "docker.log").read_text(encoding="utf-8")
        assert " up -d paper_ingestion learning_engine postgres-backup" in (
            root / "docker.log"
        ).read_text(encoding="utf-8")
        assert not (root / "backups/.lifecycle/rotation.guard").exists()
        assert not state_path.exists()
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=3)
        _release_test_rotation_guard(root)


def test_finalizing_phase_is_persisted_before_the_guard_can_be_cleared() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    promoted_tail = text.split('if [ "$phase" = "promoted" ]', 1)[1]

    assert promoted_tail.index("write_state finalizing") < promoted_tail.index(
        "finish_rotation_guard clear"
    )


def test_rotation_staging_paths_are_gitignored() -> None:
    for path in (
        "secrets/jarvis_config_key_next.txt",
        "secrets/jarvis_config_key_previous.txt",
        "secrets/jarvis_config_key.txt.rotation",
        "secrets/jarvis_config_key_rotation_state.txt",
        "secrets/.jarvis_config_key_rotation.lock",
        ".env.pre-config-key-rotation.bak",
        ".env.rotation.interrupted",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0, f"rotation secret path is not ignored: {path}"


def test_rotation_env_rewrite_reads_secret_from_file_not_process_argument() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "value_file" in text
    assert "-v v=" not in text


def test_public_runbook_uses_the_fail_fast_wrapper_not_manual_secret_steps() -> None:
    deployment = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    section = deployment.split("### Encrypted config key rotation", 1)[1].split(
        "### Docker Secrets", 1
    )[0]

    assert "bash scripts/rotate-config-key.sh" in section
    assert "Admin → Backups" in section
    for unsafe_fragment in (
        "rotate_config_rows",
        "jarvis_config_key.txt.next",
        "upsert_env_var JARVIS_CONFIG_KEY",
        'NEW_KEY="',
    ):
        assert unsafe_fragment not in section
