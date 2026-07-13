"""Real-DB contract: the maintenance watcher's resume reconcile forward-migrates.

When a restore clears, the watcher's ``_resume_after_maintenance`` runs
``run_migrations`` against a possibly-older restored schema BEFORE it resumes
background writers. This exercises that path on a real pg16.8: a pending
migration is seeded (a temp dir holding a single higher-versioned file) and the
resume reconcile must actually apply it — a mock of ``run_migrations`` would
hide a dead call (mocking it here would mask a silently-dead SQL path), so the
migration DDL genuinely PREPAREs and executes here and the table is asserted to
exist in Postgres afterward.

The reuse of the disposable pg16.8 fixture (``contract_conn`` — a per-test
transaction rolled back at teardown, wrapped as a pool via ``SharedConnPool``)
means the probe table + schema_migrations row vanish at teardown; nothing leaks
into the session-shared schema.

Verified against HEAD this session:
  app_factory.py — _resume_after_maintenance: run_migrations → invalidate caches
    → _start_worker_task.
  migrations.py:191-303 — run_migrations globs NNN_*.sql, applies unapplied
    versions inside the advisory-locked (key 42) transaction.
"""

from __future__ import annotations

import functools
from types import SimpleNamespace

import pytest
from jarvis_common import app_factory
from jarvis_common.migrations import run_migrations as _real_run_migrations
from jarvis_common.testing_db import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_PROBE_VERSION = 9999
_PROBE_TABLE = "_mw_resume_probe"
_QUEUES = ["paper_ingestion", "builtin"]


class _IdleProcrastinateApp:
    """Minimal stand-in whose run_worker_async idles; records the queues started."""

    def __init__(self) -> None:
        self.run_worker_calls: list[list[str]] = []

    async def _idle(self) -> None:
        import asyncio  # noqa: PLC0415

        await asyncio.Event().wait()

    def run_worker_async(self, *, queues, install_signal_handlers):
        self.run_worker_calls.append(queues)
        return self._idle()


async def test_resume_reconcile_applies_pending_migration(tmp_path, contract_conn, monkeypatch):
    """_resume_after_maintenance applies a pending migration for real, then resumes writers."""
    # Verified: libs/jarvis_common/jarvis_common/app_factory.py:599 (_resume_after_maintenance)
    # Verified: libs/jarvis_common/jarvis_common/migrations.py:191 (run_migrations, advisory-lock 42)
    # A pending migration living OUTSIDE the already-applied repo set: a temp dir
    # with a single higher-versioned file that creates a probe table.
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / f"{_PROBE_VERSION}_maintenance_watcher_resume_probe.sql").write_text(
        f"CREATE TABLE {_PROBE_TABLE} (id INT PRIMARY KEY);\n", encoding="utf-8"
    )

    pool = SharedConnPool(contract_conn)

    async def _probe_table() -> str | None:
        return await contract_conn.fetchval("SELECT to_regclass($1)", f"public.{_PROBE_TABLE}")

    async def _probe_marker_count() -> int:
        return await contract_conn.fetchval(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = $1", _PROBE_VERSION
        )

    # Pre-state: neither the probe table nor its schema_migrations marker exists.
    assert await _probe_table() is None
    assert await _probe_marker_count() == 0

    # Inject the pending-migration dir into the REAL run_migrations the watcher
    # calls: the SQL still PREPAREs + executes on real Postgres — only the dir is
    # controlled, so a dead resume call or bad DDL would fail this assertion.
    monkeypatch.setattr(
        app_factory,
        "run_migrations",
        functools.partial(_real_run_migrations, migrations_dir=migrations_dir),
    )

    proc = _IdleProcrastinateApp()
    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            procrastinate_app=proc,
            procrastinate_worker_task=None,
        )
    )

    await app_factory._resume_after_maintenance(app, _QUEUES)

    # The migration ran for real: the table exists and the marker is recorded.
    assert await _probe_table() == _PROBE_TABLE
    assert await _probe_marker_count() == 1

    # Writers were resumed on the same open connector.
    assert proc.run_worker_calls == [_QUEUES]
    worker = app.state.procrastinate_worker_task
    assert worker is not None and not worker.done()
    worker.cancel()
