"""Restore maintenance, outbound quarantine, and worker-pause tests.

Covers fresh and stale maintenance markers, exact recovery-route exemptions,
fail-closed quarantine reads, credential-bearing sink guards, worker resume, and
post-restore secret-rotation detection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common import app_factory
from jarvis_common import maintenance as maintenance_mod
from jarvis_common.app_factory import (
    make_maintenance_watcher_hook,
    shutdown_maintenance_watcher,
)
from jarvis_common.maintenance import (
    MaintenanceMiddleware,
    OutboundEgressBlockedError,
    ensure_outbound_egress_allowed,
    maintenance_active,
    secrets_rotated_since,
    skip_for_maintenance,
)

_MAX_AGE_S = 1800
_QUEUES = ["paper_ingestion", "builtin"]


def _make_client(sentinel: Path, destructive: Path | None = None) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        MaintenanceMiddleware,
        sentinel_path=str(sentinel),
        destructive_sentinel_path=str(destructive or sentinel.parent / ".destructive"),
        max_age_s=_MAX_AGE_S,
    )

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/admin/backups/restore/status")
    async def restore_status():
        return {"state": "running"}

    @app.get("/api/setup/status")
    async def setup_status():
        return {"setup_completed": False}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def catch_all(path: str):
        return {"path": path}

    return TestClient(app, raise_server_exceptions=True)


def test_absent_sentinel_serves_normally(tmp_path):
    client = _make_client(tmp_path / "missing.maintenance")
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_fresh_sentinel_blocks_non_exempt_with_retry_after(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/ping")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "30"
    body = json.loads(resp.content)
    assert body["detail"] == "Restore in progress"
    assert body["retry_after"] == 30


def test_health_exempt_under_fresh_sentinel(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_restore_status_exempt_under_fresh_sentinel(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/api/admin/backups/restore/status")
    assert resp.status_code == 200
    assert resp.json() == {"state": "running"}


def test_setup_status_exempt_under_fresh_sentinel(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/api/setup/status")
    assert resp.status_code == 200
    assert resp.json() == {"setup_completed": False}


def test_stale_sentinel_is_ignored(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    stale = time.time() - _MAX_AGE_S - 60
    os.utime(sentinel, (stale, stale))
    client = _make_client(sentinel)

    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_stale_destructive_sentinel_still_blocks(tmp_path):
    # No-age-gate proof: a stale SOFT sentinel would serve, but the destructive
    # sentinel blocks regardless of mtime (durable fail-closed).
    sentinel = tmp_path / ".maintenance"
    destructive = tmp_path / ".destructive"
    destructive.touch()
    stale = time.time() - _MAX_AGE_S - 3600
    os.utime(destructive, (stale, stale))
    client = _make_client(sentinel, destructive=destructive)

    resp = client.get("/ping")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "30"


def test_destructive_sentinel_exempt_paths_still_served(tmp_path):
    sentinel = tmp_path / ".maintenance"
    destructive = tmp_path / ".destructive"
    destructive.touch()
    client = _make_client(sentinel, destructive=destructive)

    assert client.get("/health").status_code == 200
    assert client.get("/api/admin/backups/restore/status").status_code == 200


def test_absent_destructive_falls_through_to_soft_logic(tmp_path):
    # Destructive absent + fresh soft sentinel -> soft logic still 503s
    # (the destructive check must not short-circuit normal serving/soft logic).
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel, destructive=tmp_path / ".destructive")

    resp = client.get("/ping")
    assert resp.status_code == 503


def test_maintenance_active_mirrors_middleware_sentinel_logic(tmp_path, monkeypatch):
    # The module-level helper follows the same lifecycle as the middleware:
    # absent -> False; fresh soft -> True; stale soft -> False; destructive -> True
    # regardless of age.
    soft = tmp_path / ".maintenance"
    destructive = tmp_path / ".destructive"
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(soft))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(destructive))

    assert maintenance_active() is False

    soft.touch()
    assert maintenance_active() is True

    stale = time.time() - _MAX_AGE_S - 60
    os.utime(soft, (stale, stale))
    assert maintenance_active() is False

    destructive.touch()
    os.utime(destructive, (stale, stale))
    assert maintenance_active() is True


# ---------------------------------------------------------------------------
# skip_for_maintenance — background-writer entry guard
# ---------------------------------------------------------------------------


def test_skip_for_maintenance_tracks_sentinel(tmp_path, monkeypatch):
    soft = tmp_path / ".maintenance"
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(soft))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))

    assert skip_for_maintenance("pulse") is False
    soft.touch()
    assert skip_for_maintenance("pulse") is True


def test_skip_for_maintenance_fails_closed_for_an_unreadable_quarantine(tmp_path, monkeypatch):
    soft = tmp_path / ".maintenance"
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(soft))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))

    assert skip_for_maintenance("pulse") is False

    quarantine.write_text("not json")
    assert skip_for_maintenance("pulse") is True


def test_outbound_egress_guard_fails_closed_for_existing_quarantine(tmp_path, monkeypatch):
    """The shared sink guard refuses even malformed quarantine state."""
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))

    ensure_outbound_egress_allowed("smtp delivery")
    quarantine.write_text("not json")

    with pytest.raises(OutboundEgressBlockedError, match="credential review"):
        ensure_outbound_egress_allowed("smtp delivery")


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/request-link",
        "/api/setup/smtp",
        "/api/jobs",
        "/api/papers/search",
        "/api/zotero/test",
        "/api/pulse/generate",
    ],
)
def test_quarantine_blocks_manual_egress_mutations(tmp_path, monkeypatch, path):
    """One ASGI policy refuses every credential-bearing manual egress family."""
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.write_text("not json")
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    client = _make_client(tmp_path / ".maintenance")

    response = client.post(path)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "This restored deployment is read-only until outbound credentials are reviewed"
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/backups/restore/status",
        "/api/setup/status",
        "/api/dashboard",
    ],
)
def test_quarantine_keeps_local_read_routes_available(tmp_path, monkeypatch, path):
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    client = _make_client(tmp_path / ".maintenance")

    assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/backups/restore/acknowledge",
        "/api/auth/api-key-session",
        "/api/auth/verify",
        "/api/auth/passkeys/capability",
        "/api/auth/passkeys/login/begin",
        "/api/auth/passkeys/login/finish",
        "/api/auth/logout",
    ],
)
def test_quarantine_keeps_exact_owner_recovery_mutations_available(tmp_path, monkeypatch, path):
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    client = _make_client(tmp_path / ".maintenance")

    assert client.post(path).status_code == 200


def test_quarantine_recovery_exemption_is_exact(tmp_path, monkeypatch):
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    client = _make_client(tmp_path / ".maintenance")

    assert client.post("/api/auth/passkeys/login/begin/extra").status_code == 503


def test_read_outbound_quarantine_returns_bound_nonsecret_state(tmp_path, monkeypatch):
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    quarantine.write_text(
        json.dumps(
            {
                "version": 1,
                "restore_id": "0123456789abcdef0123456789abcdef",
                "source": "inbox",
                "requested_at": "2026-07-21T20:00:00+00:00",
                "completed_at": "2026-07-21T20:05:00+00:00",
                "review_state": "awaiting_review",
            }
        )
    )

    state = maintenance_mod.read_outbound_quarantine()

    assert state is not None
    assert state.restore_id == "0123456789abcdef0123456789abcdef"
    assert state.source == "inbox"
    assert state.review_state == "awaiting_review"


def test_read_outbound_quarantine_rejects_malformed_existing_state(tmp_path, monkeypatch):
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    quarantine.write_text('{"restore_id":"wrong"}')

    with pytest.raises(maintenance_mod.OutboundQuarantineStateError):
        maintenance_mod.read_outbound_quarantine()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_read_outbound_quarantine_rejects_linked_state(tmp_path, monkeypatch, link_kind):
    real = tmp_path / "real-quarantine.json"
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    real.write_text(
        json.dumps(
            {
                "version": 1,
                "restore_id": "0123456789abcdef0123456789abcdef",
                "source": "inbox",
                "requested_at": "2026-07-21T20:00:00+00:00",
                "completed_at": "2026-07-21T20:05:00+00:00",
                "review_state": "awaiting_review",
            }
        )
    )
    if link_kind == "symlink":
        quarantine.symlink_to(real)
    else:
        os.link(real, quarantine)

    with pytest.raises(maintenance_mod.OutboundQuarantineStateError):
        maintenance_mod.read_outbound_quarantine()


# ---------------------------------------------------------------------------
# Maintenance watcher — pause the worker LOOP on restore, reconcile on resume
# ---------------------------------------------------------------------------


class _FakeProcrastinateApp:
    """Records worker starts; run_worker_async returns a coroutine that idles."""

    def __init__(self) -> None:
        self.run_worker_calls: list[list[str]] = []

    async def _idle(self) -> None:
        await asyncio.Event().wait()  # runs until the task is cancelled

    def run_worker_async(self, *, queues, install_signal_handlers):
        self.run_worker_calls.append(queues)
        return self._idle()


def _make_worker_app() -> tuple[SimpleNamespace, _FakeProcrastinateApp]:
    proc = _FakeProcrastinateApp()
    app = SimpleNamespace(
        state=SimpleNamespace(
            procrastinate_app=proc,
            procrastinate_worker_task=None,
            db_pool=SimpleNamespace(),
        )
    )
    return app, proc


def _point_sentinels(tmp_path, monkeypatch) -> Path:
    """Route maintenance_active() at tmp_path sentinels; return the soft path."""
    soft = tmp_path / ".maintenance"
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(soft))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    return soft


async def _cancel(task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_watcher_step_pauses_worker_loop_on_activation(tmp_path, monkeypatch):
    """inactive→active cancels the whole worker task with no resume side-effects."""
    soft = _point_sentinels(tmp_path, monkeypatch)
    app, proc = _make_worker_app()
    app_factory._start_worker_task(app, _QUEUES)
    worker = app.state.procrastinate_worker_task
    assert worker is not None and not worker.done()

    # Still inactive: a poll is a no-op — the worker keeps running.
    assert await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=False) is False
    assert app.state.procrastinate_worker_task is worker

    # Restore raises the sentinel: the loop is cancelled, no worker restarted.
    soft.touch()
    assert await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=False) is True
    assert app.state.procrastinate_worker_task is None
    assert worker.cancelled()
    assert len(proc.run_worker_calls) == 1  # only the initial start; no resume


async def test_watcher_step_reconciles_then_resumes_on_clear(tmp_path, monkeypatch):
    """active→inactive runs migrations + invalidates caches THEN restarts the worker."""
    _point_sentinels(tmp_path, monkeypatch)  # both absent → inactive
    run_migrations_spy = AsyncMock()
    inval_login = MagicMock()
    inval_ctx = MagicMock()
    monkeypatch.setattr(app_factory, "run_migrations", run_migrations_spy)
    monkeypatch.setattr(app_factory, "invalidate_api_key_login_cache", inval_login)
    monkeypatch.setattr(app_factory, "invalidate_effective_num_ctx_cache", inval_ctx)

    app, proc = _make_worker_app()  # paused: no worker task
    assert app.state.procrastinate_worker_task is None

    result = await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=True)

    assert result is False
    run_migrations_spy.assert_awaited_once_with(app.state.db_pool)
    inval_login.assert_called_once_with()
    inval_ctx.assert_called_once_with()
    new_worker = app.state.procrastinate_worker_task
    assert new_worker is not None and not new_worker.done()
    assert proc.run_worker_calls == [_QUEUES]
    await _cancel(new_worker)


async def test_watcher_loop_survives_a_tick_exception(tmp_path, monkeypatch):
    """A raised exception in one poll is logged and the loop keeps polling."""
    _point_sentinels(tmp_path, monkeypatch)
    app, _ = _make_worker_app()
    calls: list[int] = []

    async def _flaky_step(app, queues, *, was_active, started_at=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return False

    monkeypatch.setattr(app_factory, "_maintenance_watcher_step", _flaky_step)
    loop_task = asyncio.create_task(app_factory._maintenance_watcher_loop(app, _QUEUES, 0.01))
    await asyncio.sleep(0.05)

    assert not loop_task.done(), "watcher loop must survive a tick exception"
    assert len(calls) >= 2, "watcher must keep polling after an exception"
    await _cancel(loop_task)


async def test_watcher_loop_pauses_immediately_when_active_at_start(tmp_path, monkeypatch):
    """A service that boots mid-restore pauses the worker before the first poll."""
    soft = _point_sentinels(tmp_path, monkeypatch)
    soft.touch()  # maintenance already active at startup
    app, _ = _make_worker_app()
    app_factory._start_worker_task(app, _QUEUES)
    worker = app.state.procrastinate_worker_task

    loop_task = asyncio.create_task(app_factory._maintenance_watcher_loop(app, _QUEUES, 5.0))
    for _ in range(50):
        if app.state.procrastinate_worker_task is None:
            break
        await asyncio.sleep(0.005)

    assert app.state.procrastinate_worker_task is None
    assert worker.cancelled()
    await _cancel(loop_task)


async def test_watcher_hook_starts_task_and_shutdown_cancels_it(tmp_path, monkeypatch):
    """The init hook spawns the watcher; the teardown cancels and clears it."""
    _point_sentinels(tmp_path, monkeypatch)
    app, _ = _make_worker_app()

    await make_maintenance_watcher_hook(_QUEUES, poll_interval_s=5.0)(app)
    watcher = app.state.maintenance_watcher_task
    assert watcher is not None and not watcher.done()

    await shutdown_maintenance_watcher(app)
    assert app.state.maintenance_watcher_task is None
    assert watcher.done()


# ---------------------------------------------------------------------------
# Secrets-rotation self-restart (off-host restore) — decision path only
# ---------------------------------------------------------------------------


def test_secrets_rotated_since_marker_lifecycle(tmp_path, monkeypatch):
    marker = tmp_path / ".secrets_rotated"
    monkeypatch.setattr(maintenance_mod, "SECRETS_ROTATED_MARKER", marker)
    started = 1000.0

    assert secrets_rotated_since(started) is False  # marker absent
    marker.write_text("999\n")
    assert secrets_rotated_since(started) is False  # older than start
    marker.write_text("1001\n")
    assert secrets_rotated_since(started) is True  # newer than start
    marker.write_text("not-an-int")
    assert secrets_rotated_since(started) is False  # malformed never raises


async def test_watcher_step_self_restarts_once_on_newer_marker(tmp_path, monkeypatch):
    """active→inactive with a marker newer than started_at fires exactly one restart."""
    _point_sentinels(tmp_path, monkeypatch)  # both absent → inactive
    migrate = AsyncMock()
    monkeypatch.setattr(app_factory, "run_migrations", migrate)
    monkeypatch.setattr(app_factory, "invalidate_api_key_login_cache", MagicMock())
    monkeypatch.setattr(app_factory, "invalidate_effective_num_ctx_cache", MagicMock())
    restart = MagicMock()
    monkeypatch.setattr(app_factory, "_trigger_secrets_rotation_restart", restart)
    marker = tmp_path / ".secrets_rotated"
    monkeypatch.setattr(maintenance_mod, "SECRETS_ROTATED_MARKER", marker)
    started = 1000.0

    marker.write_text("2000\n")  # newer than start → restart
    app, _ = _make_worker_app()
    await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=True, started_at=started)
    restart.assert_called_once_with()
    migrate.assert_not_awaited()  # rotation path self-restarts BEFORE the DB-touching resume
    assert app.state.procrastinate_worker_task is None  # resume + worker-start were skipped

    restart.reset_mock()
    marker.write_text("500\n")  # older than start → no restart
    app2, _ = _make_worker_app()
    await app_factory._maintenance_watcher_step(app2, _QUEUES, was_active=True, started_at=started)
    restart.assert_not_called()
    await _cancel(app2.state.procrastinate_worker_task)


async def test_watcher_step_self_restarts_even_when_migrations_would_fail(tmp_path, monkeypatch):
    """Cross-host regression: the app pool still holds the pre-rotation password, so
    ``run_migrations``' acquire auth-fails until we restart. The restart must fire
    regardless — it used to be sequenced AFTER the failing resume and so never ran."""
    _point_sentinels(tmp_path, monkeypatch)
    migrate = AsyncMock(side_effect=RuntimeError("password authentication failed"))
    monkeypatch.setattr(app_factory, "run_migrations", migrate)
    monkeypatch.setattr(app_factory, "invalidate_api_key_login_cache", MagicMock())
    monkeypatch.setattr(app_factory, "invalidate_effective_num_ctx_cache", MagicMock())
    restart = MagicMock()
    monkeypatch.setattr(app_factory, "_trigger_secrets_rotation_restart", restart)
    marker = tmp_path / ".secrets_rotated"
    monkeypatch.setattr(maintenance_mod, "SECRETS_ROTATED_MARKER", marker)
    marker.write_text("2000\n")  # newer than start
    started = 1000.0

    app, _ = _make_worker_app()
    await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=True, started_at=started)
    restart.assert_called_once_with()
    migrate.assert_not_awaited()  # never reached the DB op — restart short-circuits it
    assert app.state.procrastinate_worker_task is None


async def test_watcher_step_no_restart_when_started_at_absent(tmp_path, monkeypatch):
    """The legacy call shape (no started_at) never self-restarts, marker or not."""
    _point_sentinels(tmp_path, monkeypatch)
    monkeypatch.setattr(app_factory, "run_migrations", AsyncMock())
    monkeypatch.setattr(app_factory, "invalidate_api_key_login_cache", MagicMock())
    monkeypatch.setattr(app_factory, "invalidate_effective_num_ctx_cache", MagicMock())
    restart = MagicMock()
    monkeypatch.setattr(app_factory, "_trigger_secrets_rotation_restart", restart)
    marker = tmp_path / ".secrets_rotated"
    marker.write_text("9999999999\n")
    monkeypatch.setattr(maintenance_mod, "SECRETS_ROTATED_MARKER", marker)

    app, _ = _make_worker_app()
    await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=True)
    restart.assert_not_called()
    await _cancel(app.state.procrastinate_worker_task)
