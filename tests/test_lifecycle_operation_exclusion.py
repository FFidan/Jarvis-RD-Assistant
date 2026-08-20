"""Black-box coverage for the host/sidecar lifecycle-operation boundary.

These tests deliberately use the real shell entrypoints with a temporary directory
standing in for the private postgres_backups named volume. They stop before any
restore work can reach Postgres or Qdrant:
the assertions are about admission, durable recovery ownership, and the update
holder yielding its pre-mutation reservation to a restore request.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESTORE = REPO_ROOT / "scripts" / "restore.sh"
LIFECYCLE_HELPER = REPO_ROOT / "scripts" / "backup-lifecycle.sh"
SETUP_LIB = REPO_ROOT / "scripts" / "setup_lib.sh"


def _environment(root: Path, destructive_log: Path) -> dict[str, str]:
    trigger = root / "trigger"
    inbox = root / "inbox"
    secrets = root / "secrets"
    fake_bin = root / "fake-bin"
    for directory in (trigger, inbox, secrets, fake_bin, root / "backups"):
        directory.mkdir(parents=True, exist_ok=True)
    for command in ("psql", "pg_restore", "curl", "docker"):
        target = fake_bin / command
        target.write_text(
            f"#!/usr/bin/env bash\nprintf '%s\\n' {command} >> {destructive_log!s}\nexit 97\n",
            encoding="utf-8",
        )
        target.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "BACKUP_TRIGGER_DIR": str(trigger),
            "BACKUP_DIR": str(root / "backups"),
            "RESTORE_INBOX_DIR": str(inbox),
            "HOST_SECRETS_DIR": str(secrets),
            "JARVIS_BACKUP_TRIGGER_DIR": str(trigger),
            "JARVIS_BACKUP_DIR": str(root / "backups"),
            "JARVIS_CLI_CONFIG_DIR": str(root / "cli-config"),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    return env


def _run_restore(root: Path, destructive_log: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RESTORE), *args],
        cwd=REPO_ROOT,
        env=_environment(root, destructive_log),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def _run_lifecycle(
    root: Path, destructive_log: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(LIFECYCLE_HELPER), *args],
        cwd=REPO_ROOT,
        env=_environment(root, destructive_log),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def _start_update_holder(
    root: Path, destructive_log: Path, guard_id: str, timeout: str = "10"
) -> subprocess.Popen[str]:
    reserved = _run_lifecycle(root, destructive_log, "reserve-update", guard_id)
    assert reserved.returncode == 0, reserved.stderr
    holder = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-update", guard_id, timeout],
        cwd=REPO_ROOT,
        env=_environment(root, destructive_log),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lifecycle = root / "backups" / ".lifecycle"
    _wait_for(lifecycle / "update.guard", guard_id, holder)
    _wait_for(lifecycle / "operation.state", f"update-preparing:{guard_id}", holder)
    return holder


def _wait_for(path: Path, expected: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip() == expected:
            return
        if process.poll() is not None:
            raise AssertionError(
                f"lifecycle holder exited early ({process.returncode}): {process.stderr.read()}"
            )
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path} to contain {expected!r}")


def _write_admission_inputs(root: Path) -> dict[Path, bytes]:
    trigger = root / "trigger"
    inbox = root / "inbox"
    inputs = {
        trigger / ".restore_request.json": b'{"timestamp":"not-a-timestamp"}\n',
        trigger / ".restore_status.json": b'{"state":"existing"}\n',
        trigger / ".maintenance": b"maintenance\n",
        inbox / "operator_key": b"one-time-secret\n",
    }
    for path, contents in inputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    return inputs


def test_live_foreign_lock_refuses_restore_without_mutation_then_retry_progresses(
    tmp_path: Path,
) -> None:
    """A live foreign holder leaves every restore-owned input byte-for-byte intact."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    _environment(tmp_path, destructive_log)
    inputs = _write_admission_inputs(tmp_path)
    lifecycle = tmp_path / "backups" / ".lifecycle"
    lifecycle.mkdir(mode=0o700)
    lock = lifecycle / "operation.lock"
    state = lifecycle / "operation.state"
    lock.touch()
    state.write_text(f"setup:{'b' * 32}\n", encoding="utf-8")

    with lock.open("rb") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = _run_restore(tmp_path, destructive_log)
        assert refused.returncode == 0, refused.stderr
        assert all(path.read_bytes() == expected for path, expected in inputs.items())
        assert destructive_log.read_text(encoding="utf-8") == ""
        assert not (lifecycle / "update.control").exists()
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)

    # A completed foreign owner removes its durable state; the exact same request
    # then reaches normal validation and is consumed, proving no late activation
    # occurred while the foreign lock was held.
    state.unlink()
    retried = _run_restore(tmp_path, destructive_log)
    assert retried.returncode != 0, "malformed retained requests must fail closed"
    assert not (tmp_path / "trigger" / ".restore_request.json").exists()
    assert (tmp_path / "trigger" / ".restore_status.json").read_bytes() != inputs[
        tmp_path / "trigger" / ".restore_status.json"
    ]
    assert destructive_log.read_text(encoding="utf-8") == ""


def test_restore_state_is_adoptable_only_by_restore_and_retained_after_destructive_recovery(
    tmp_path: Path,
) -> None:
    """Clean recovery clears restore ownership; destructive recovery retains it."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    _environment(tmp_path, destructive_log)
    lifecycle = tmp_path / "backups" / ".lifecycle"
    lifecycle.mkdir(mode=0o700)
    state = lifecycle / "operation.state"
    state.write_text("restore\n", encoding="utf-8")

    clean = _run_restore(tmp_path, destructive_log, "--recover")
    assert clean.returncode == 0, clean.stderr
    assert not state.exists(), "pre-drop recovery must release the restore state"
    assert destructive_log.read_text(encoding="utf-8") == ""

    state.write_text("restore\n", encoding="utf-8")
    (tmp_path / "trigger" / ".destructive").write_text("entered-drop-window\n", encoding="utf-8")
    destructive = _run_restore(tmp_path, destructive_log, "--recover")
    assert destructive.returncode == 0, destructive.stderr
    assert state.read_text(encoding="utf-8") == "restore\n"
    assert destructive_log.read_text(encoding="utf-8") == ""

    foreign = _run_lifecycle(tmp_path, destructive_log, "reserve-host", "setup", "b" * 32)
    assert foreign.returncode != 0
    assert "another lifecycle operation" in foreign.stderr


def test_preparing_update_yields_to_restore_without_late_activation(tmp_path: Path) -> None:
    """A restore request wins against an update that has not yet promoted itself."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    env = _environment(tmp_path, destructive_log)
    inputs = _write_admission_inputs(tmp_path)
    guard_id = "a" * 32
    reserved = _run_lifecycle(tmp_path, destructive_log, "reserve-update", guard_id)
    assert reserved.returncode == 0, reserved.stderr

    holder = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-update", guard_id, "5"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        trigger = tmp_path / "trigger"
        lifecycle = tmp_path / "backups" / ".lifecycle"
        state = lifecycle / "operation.state"
        _wait_for(lifecycle / "update.guard", guard_id, holder)
        _wait_for(state, f"update-preparing:{guard_id}", holder)

        refused = _run_restore(tmp_path, destructive_log)
        assert refused.returncode == 0, refused.stderr
        assert all(path.read_bytes() == expected for path, expected in inputs.items())
        assert destructive_log.read_text(encoding="utf-8") == ""

        assert holder.wait(timeout=5) == 0, holder.stderr.read()
        for path in (
            lifecycle / "update.guard",
            lifecycle / "update.reservation",
            lifecycle / "update.control",
            state,
        ):
            assert not path.exists(), f"yield left stale lifecycle state: {path}"

        # The retained request now wins admission and reaches validation.  It is
        # intentionally malformed so no database or search-index command can run.
        retried = _run_restore(tmp_path, destructive_log)
        assert retried.returncode != 0, "malformed retained requests must fail closed"
        assert not (trigger / ".restore_request.json").exists()
        assert destructive_log.read_text(encoding="utf-8") == ""
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_restore_yield_cannot_be_overwritten_by_promotion_or_release(tmp_path: Path) -> None:
    """Once restore wins admission, update commands join or lose without replacing it."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    guard_id = "1" * 32
    lifecycle = tmp_path / "backups" / ".lifecycle"
    control = lifecycle / "update.control"
    holder = _start_update_holder(tmp_path, destructive_log, guard_id)
    stopped = False
    try:
        holder.send_signal(signal.SIGSTOP)
        stopped = True
        refused = _run_restore(tmp_path, destructive_log)
        assert refused.returncode == 0, refused.stderr
        assert "asking the preparing update to yield" in refused.stderr
        assert control.read_text(encoding="utf-8").strip() == f"{guard_id}:yield-restore"

        promoted = _run_lifecycle(tmp_path, destructive_log, "promote-update", guard_id)
        assert promoted.returncode != 0
        assert control.read_text(encoding="utf-8").strip() == f"{guard_id}:yield-restore"

        released = _run_lifecycle(tmp_path, destructive_log, "release-update", guard_id, "retain")
        assert released.returncode == 0, released.stderr
        assert control.read_text(encoding="utf-8").strip() == f"{guard_id}:yield-restore"

        holder.send_signal(signal.SIGCONT)
        stopped = False
        assert holder.wait(timeout=5) == 0, holder.stderr.read()
        for path in (
            lifecycle / "update.guard",
            lifecycle / "update.reservation",
            control,
            lifecycle / "operation.state",
        ):
            assert not path.exists(), f"yield left stale lifecycle state: {path}"
    finally:
        if stopped and holder.poll() is None:
            holder.send_signal(signal.SIGCONT)
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_queued_promotion_cannot_be_overwritten_by_restore_yield(tmp_path: Path) -> None:
    """A promotion admitted first keeps update priority even before the holder consumes it."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    guard_id = "2" * 32
    lifecycle = tmp_path / "backups" / ".lifecycle"
    control = lifecycle / "update.control"
    state = lifecycle / "operation.state"
    holder = _start_update_holder(tmp_path, destructive_log, guard_id)
    promoter: subprocess.Popen[str] | None = None
    stopped = False
    try:
        holder.send_signal(signal.SIGSTOP)
        stopped = True
        promoter = subprocess.Popen(
            ["bash", str(LIFECYCLE_HELPER), "promote-update", guard_id],
            cwd=REPO_ROOT,
            env=_environment(tmp_path, destructive_log),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for(control, f"{guard_id}:promote", promoter)

        refused = _run_restore(tmp_path, destructive_log)
        assert refused.returncode == 0, refused.stderr
        assert "asking the preparing update to yield" not in refused.stderr
        assert control.read_text(encoding="utf-8").strip() == f"{guard_id}:promote"

        holder.send_signal(signal.SIGCONT)
        stopped = False
        promoter_stdout, promoter_stderr = promoter.communicate(timeout=5)
        assert promoter.returncode == 0, f"{promoter_stdout}\n{promoter_stderr}"
        _wait_for(state, f"update:{guard_id}", holder)
        released = _run_lifecycle(tmp_path, destructive_log, "release-update", guard_id)
        assert released.returncode == 0, released.stderr
        assert holder.wait(timeout=5) == 0, holder.stderr.read()
    finally:
        if stopped and holder.poll() is None:
            holder.send_signal(signal.SIGCONT)
        if promoter is not None and promoter.poll() is None:
            promoter.terminate()
            promoter.wait(timeout=5)
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_timed_out_promotion_is_cancelled_before_holder_can_activate_it(
    tmp_path: Path,
) -> None:
    """A requester that gives up removes its still-queued promotion under admission."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    guard_id = "3" * 32
    lifecycle = tmp_path / "backups" / ".lifecycle"
    control = lifecycle / "update.control"
    holder = _start_update_holder(tmp_path, destructive_log, guard_id)
    stopped = False
    try:
        holder.send_signal(signal.SIGSTOP)
        stopped = True
        promoted = _run_lifecycle(tmp_path, destructive_log, "promote-update", guard_id)
        assert promoted.returncode != 0
        assert not control.exists(), "timed-out promotion remained queued for late activation"
        assert (lifecycle / "operation.state").read_text(encoding="utf-8").strip() == (
            f"update-preparing:{guard_id}"
        )

        holder.send_signal(signal.SIGCONT)
        stopped = False
        time.sleep(0.2)
        assert (lifecycle / "operation.state").read_text(encoding="utf-8").strip() == (
            f"update-preparing:{guard_id}"
        )
        released = _run_lifecycle(tmp_path, destructive_log, "release-update", guard_id)
        assert released.returncode == 0, released.stderr
        assert holder.wait(timeout=5) == 0, holder.stderr.read()
    finally:
        if stopped and holder.poll() is None:
            holder.send_signal(signal.SIGCONT)
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_restore_yield_admitted_at_holder_deadline_is_not_orphaned(tmp_path: Path) -> None:
    """Deadline cleanup consumes a winning yield instead of abandoning its control file."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    guard_id = "4" * 32
    lifecycle = tmp_path / "backups" / ".lifecycle"
    holder = _start_update_holder(tmp_path, destructive_log, guard_id, timeout="1")
    stopped = False
    try:
        holder.send_signal(signal.SIGSTOP)
        stopped = True
        time.sleep(1.1)
        refused = _run_restore(tmp_path, destructive_log)
        assert refused.returncode == 0, refused.stderr
        assert "asking the preparing update to yield" in refused.stderr

        holder.send_signal(signal.SIGCONT)
        stopped = False
        assert holder.wait(timeout=5) == 0, holder.stderr.read()
        for path in (
            lifecycle / "update.guard",
            lifecycle / "update.reservation",
            lifecycle / "update.control",
            lifecycle / "operation.state",
        ):
            assert not path.exists(), f"deadline yield left stale state: {path}"
    finally:
        if stopped and holder.poll() is None:
            holder.send_signal(signal.SIGCONT)
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_promotion_refuses_if_holder_dies_during_durable_transition(tmp_path: Path) -> None:
    """Durable promoted state is not sufficient when its exclusive holder has died."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    guard_id = "5" * 32
    lifecycle = tmp_path / "backups" / ".lifecycle"
    control = lifecycle / "update.control"
    state = lifecycle / "operation.state"
    holder = _start_update_holder(tmp_path, destructive_log, guard_id)
    promoter: subprocess.Popen[str] | None = None
    stopped = False
    try:
        holder.send_signal(signal.SIGSTOP)
        stopped = True
        promoter = subprocess.Popen(
            ["bash", str(LIFECYCLE_HELPER), "promote-update", guard_id],
            cwd=REPO_ROOT,
            env=_environment(tmp_path, destructive_log),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for(control, f"{guard_id}:promote", promoter)

        admission = lifecycle / "operation-admission.lock"
        with admission.open("a+b") as transition_lock:
            fcntl.flock(transition_lock.fileno(), fcntl.LOCK_EX)
            staged_state = lifecycle / "operation.state.test-transition"
            staged_state.write_text(f"update:{guard_id}\n", encoding="utf-8")
            staged_state.replace(state)
            holder.kill()
            holder.wait(timeout=5)
            stopped = False
            time.sleep(0.1)
            fcntl.flock(transition_lock.fileno(), fcntl.LOCK_UN)

        promoter_stdout, promoter_stderr = promoter.communicate(timeout=5)
        assert promoter.returncode != 0, f"{promoter_stdout}\n{promoter_stderr}"
        assert state.read_text(encoding="utf-8").strip() == f"update:{guard_id}"
        assert not control.exists(), "dead-holder promotion request remained queued"
    finally:
        if stopped and holder.poll() is None:
            holder.send_signal(signal.SIGCONT)
        if promoter is not None and promoter.poll() is None:
            promoter.terminate()
            promoter.wait(timeout=5)
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_preparing_holder_timeout_clears_identity_before_retry(tmp_path: Path) -> None:
    """An expired pre-mutation holder cannot be adopted and reactivated later."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    expired_id = "6" * 32
    retry_id = "7" * 32
    lifecycle = tmp_path / "backups" / ".lifecycle"
    # The holder's expiry has to outlast starting a subprocess and watching it take
    # the guard, which is real process startup: at one second a loaded machine
    # expires the holder before that setup finishes, and the case fails having
    # asserted nothing. Three seconds still expires well inside the wait below,
    # which is the behaviour the case is actually about.
    expired = _start_update_holder(tmp_path, destructive_log, expired_id, timeout="3")
    assert expired.wait(timeout=30) != 0
    for path in (
        lifecycle / "update.guard",
        lifecycle / "update.reservation",
        lifecycle / "update.control",
        lifecycle / "operation.state",
    ):
        assert not path.exists(), f"expired preparing holder left resumable state: {path}"

    retry = _start_update_holder(tmp_path, destructive_log, retry_id)
    try:
        released = _run_lifecycle(tmp_path, destructive_log, "release-update", retry_id)
        assert released.returncode == 0, released.stderr
        assert retry.wait(timeout=5) == 0, retry.stderr.read()
    finally:
        if retry.poll() is None:
            retry.terminate()
            retry.wait(timeout=5)


def test_concurrent_restore_yield_and_update_promotion_have_one_winner(
    tmp_path: Path,
) -> None:
    """Real competing subprocesses never both receive mutation admission."""
    for attempt in range(16):
        root = tmp_path / f"race-{attempt}"
        destructive_log = root / "destructive.log"
        destructive_log.parent.mkdir(parents=True)
        destructive_log.write_text("", encoding="utf-8")
        guard_id = f"{attempt:032x}"
        lifecycle = root / "backups" / ".lifecycle"
        state = lifecycle / "operation.state"
        gate = root / "start-race"
        holder = _start_update_holder(root, destructive_log, guard_id)
        promotion: subprocess.Popen[str] | None = None
        restore: subprocess.Popen[str] | None = None
        try:
            barrier = 'gate="$1"; shift; while [ ! -e "$gate" ]; do sleep 0.001; done; exec "$@"'
            promotion = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    barrier,
                    "race-promotion",
                    str(gate),
                    "bash",
                    str(LIFECYCLE_HELPER),
                    "promote-update",
                    guard_id,
                ],
                cwd=REPO_ROOT,
                env=_environment(root, destructive_log),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            restore = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    barrier,
                    "race-restore",
                    str(gate),
                    "bash",
                    str(RESTORE),
                ],
                cwd=REPO_ROOT,
                env=_environment(root, destructive_log),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            gate.touch()
            promotion_stdout, promotion_stderr = promotion.communicate(timeout=8)
            _, restore_stderr = restore.communicate(timeout=8)
            promotion_won = promotion.returncode == 0
            restore_won = "asking the preparing update to yield" in restore_stderr
            assert promotion_won != restore_won, (
                f"race {attempt}: promotion rc={promotion.returncode}, "
                f"promotion stderr={promotion_stderr!r}, restore stderr={restore_stderr!r}"
            )

            if promotion_won:
                _wait_for(state, f"update:{guard_id}", holder)
                released = _run_lifecycle(root, destructive_log, "release-update", guard_id)
                assert released.returncode == 0, released.stderr
            assert holder.wait(timeout=5) == 0, (
                f"{promotion_stdout}\n{promotion_stderr}\n{holder.stderr.read()}"
            )
            for path in (
                lifecycle / "update.guard",
                lifecycle / "update.reservation",
                lifecycle / "update.control",
                state,
            ):
                assert not path.exists(), f"race {attempt} left stale state: {path}"
        finally:
            for process in (promotion, restore):
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            if holder.poll() is None:
                holder.terminate()
                holder.wait(timeout=5)


def test_cancelled_host_reservation_cannot_activate_later(tmp_path: Path) -> None:
    """A delayed detached helper never recreates a reservation the caller cancelled."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    guard_id = "c" * 32
    reserve = _run_lifecycle(tmp_path, destructive_log, "reserve-host", "setup", guard_id)
    assert reserve.returncode == 0, reserve.stderr
    cancel = _run_lifecycle(
        tmp_path,
        destructive_log,
        "cancel-host-reservation",
        "setup",
        guard_id,
    )
    assert cancel.returncode == 0, cancel.stderr
    delayed = _run_lifecycle(
        tmp_path,
        destructive_log,
        "hold-host",
        "setup",
        guard_id,
        "1",
    )
    assert delayed.returncode != 0
    lifecycle = tmp_path / "backups" / ".lifecycle"
    assert not (lifecycle / "host.reservation").exists()
    assert not (lifecycle / "operation.state").exists()


def test_dead_host_holder_is_adoptable_only_by_its_exact_identity(tmp_path: Path) -> None:
    """A crash retry adopts its own state without opening cross-operation recovery."""
    destructive_log = tmp_path / "destructive.log"
    destructive_log.write_text("", encoding="utf-8")
    env = _environment(tmp_path, destructive_log)
    guard_id = "d" * 32
    other_id = "e" * 32
    lifecycle = tmp_path / "backups" / ".lifecycle"

    reserved = _run_lifecycle(tmp_path, destructive_log, "reserve-host", "setup", guard_id)
    assert reserved.returncode == 0, reserved.stderr
    # Own process group, so the shell and the `sleep` it is executing can be
    # killed together below.
    crashed = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-host", "setup", guard_id, "30"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _wait_for(lifecycle / "operation.state", f"setup:{guard_id}", crashed)
    # The holder polls with `sleep 0.1`, and that child inherits the flock'd
    # descriptors. Signalling the shell alone leaves the sleep holding both the
    # operation and the reservation lock for up to 0.1s after the shell has been
    # reaped, which is long enough for the retry below to be refused the lock it
    # is entitled to. Kill the group so the descriptors go with it.
    os.killpg(os.getpgid(crashed.pid), signal.SIGTERM)
    crashed.wait(timeout=5)

    # The locks are released with the group above; this loop now only confirms
    # the helper reports the crashed holder as gone before durable-state
    # adoption is tested.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        old_status = _run_lifecycle(
            tmp_path,
            destructive_log,
            "host-status",
            "setup",
            guard_id,
        )
        if old_status.returncode != 0:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("crashed holder did not release its lock")

    for kind, candidate_id in (("setup", other_id), ("uninstall", guard_id)):
        foreign = _run_lifecycle(
            tmp_path,
            destructive_log,
            "reserve-host",
            kind,
            candidate_id,
        )
        assert foreign.returncode != 0
        assert "another lifecycle operation" in foreign.stderr

    retry_reservation = _run_lifecycle(
        tmp_path,
        destructive_log,
        "reserve-host",
        "setup",
        guard_id,
    )
    assert retry_reservation.returncode == 0, retry_reservation.stderr
    retry = subprocess.Popen(
        ["bash", str(LIFECYCLE_HELPER), "hold-host", "setup", guard_id, "5"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            active = _run_lifecycle(
                tmp_path,
                destructive_log,
                "host-status",
                "setup",
                guard_id,
            )
            if active.returncode == 0:
                break
            if retry.poll() is not None:
                raise AssertionError(f"retry holder exited early: {retry.stderr.read()}")
            time.sleep(0.02)
        else:
            raise AssertionError("retry never adopted the retained setup identity")

        released = _run_lifecycle(
            tmp_path,
            destructive_log,
            "release-host",
            "setup",
            guard_id,
            "clear",
        )
        assert released.returncode == 0, released.stderr
        assert retry.wait(timeout=5) == 0, retry.stderr.read()
        assert not (lifecycle / "operation.state").exists()
        assert not (lifecycle / "host.reservation").exists()
    finally:
        if retry.poll() is None:
            retry.terminate()
            retry.wait(timeout=5)


def test_lifecycle_protocol_never_uses_bind_secrets_or_app_writable_trigger() -> None:
    """Static contract: private protocol files stay under /backups/.lifecycle."""
    helper = LIFECYCLE_HELPER.read_text(encoding="utf-8")
    setup_lib = SETUP_LIB.read_text(encoding="utf-8")
    restore = RESTORE.read_text(encoding="utf-8")
    forbidden = (
        ".jarvis-lifecycle-operation",
        "${TRIGGER_DIR}/.update-lifecycle",
        "${TRIGGER_DIR}/.config-key-rotation",
        "${TRIGGER_DIR}/.restore_timeout",
        "${TRIGGER_DIR}/.restore_swap_state",
    )
    for needle in forbidden:
        assert needle not in helper
        assert needle not in restore
    assert "secrets/.jarvis-lifecycle-operation" not in setup_lib
    for marker in (
        'OPERATION_STATE="${LOCK_DIR}/operation.state"',
        'HOST_RESERVATION="${LOCK_DIR}/host.reservation"',
        'UPDATE_CONTROL="${LOCK_DIR}/update.control"',
        'ROTATION_CONTROL="${LOCK_DIR}/rotation.control"',
    ):
        assert marker in helper
    for marker in (
        'SWAP_STATE_FILE="${LOCK_DIR}/restore-swap-state.json"',
        'RESTORE_TIMEOUT_FILE="${LOCK_DIR}/restore-timeout"',
    ):
        assert marker in restore
