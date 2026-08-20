"""Live-PostgreSQL contract for restore resume's read-only schema check."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from jarvis_common import app_factory
from jarvis_common.migrations import MigrationCheck
from jarvis_common.testing_db import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_QUEUES = ["paper_ingestion", "builtin"]


class _IdleProcrastinateApp:
    """Minimal worker stand-in that records successful resume starts."""

    def __init__(self) -> None:
        self.run_worker_calls: list[list[str]] = []

    async def _idle(self) -> None:
        import asyncio  # noqa: PLC0415

        await asyncio.Event().wait()

    def run_worker_async(self, *, queues, install_signal_handlers):
        self.run_worker_calls.append(queues)
        return self._idle()


async def test_resume_reconcile_checks_schema_without_mutating_it(contract_conn):
    """A successful restore resume reads migration state before restarting writers.

    # Verified: libs/jarvis_common/jarvis_common/app_factory.py:_resume_after_maintenance
    # Verified: libs/jarvis_common/jarvis_common/migrations.py:check_migrations
    """
    pool = SharedConnPool(contract_conn)
    before = await contract_conn.fetch("SELECT version, sha256 FROM ops.schema_migrations")
    proc = _IdleProcrastinateApp()
    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            procrastinate_app=proc,
            procrastinate_worker_task=None,
        )
    )

    await app_factory._resume_after_maintenance(app, _QUEUES)

    after = await contract_conn.fetch("SELECT version, sha256 FROM ops.schema_migrations")
    assert after == before
    assert isinstance(app.state.migration_check, MigrationCheck)
    assert app.state.migration_check.integrity == "ok"
    assert proc.run_worker_calls == [_QUEUES]
    app.state.procrastinate_worker_task.cancel()
