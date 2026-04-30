"""Shared test fixtures for learning_engine tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies are installed on the host venv — no stubs or
path manipulation needed here.
"""

import os
import shutil
import subprocess
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import jarvis_common.jobs as _jobs_module
import pytest

# ---------------------------------------------------------------------------
# _HANDLERS isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _reset_job_handlers():
    """Snapshot and restore jarvis_common.jobs._HANDLERS around each test.

    Use this fixture in any test that registers new job handlers, to prevent
    cross-test _HANDLERS state pollution.  Never rely on global _HANDLERS state
    across tests — always opt-in to this fixture when your test touches handler
    registration.
    """
    snapshot = dict(_jobs_module._HANDLERS)
    yield
    _jobs_module._HANDLERS.clear()
    _jobs_module._HANDLERS.update(snapshot)


# ---------------------------------------------------------------------------
# FakeRecord + shared fixtures
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Unified asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _make_pool_and_conn():
    """Create mock asyncpg Pool + Connection with transaction support."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord


# ---------------------------------------------------------------------------
# Live PostgreSQL fixture (opt-in via JARVIS_RUN_LIVE_PG=1)
# ---------------------------------------------------------------------------


def _docker(
    args: list[str],
    *,
    check: bool = True,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a Docker CLI command for opt-in live PostgreSQL tests."""
    return subprocess.run(
        ["docker", *args],
        check=check,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


@pytest.fixture()
def live_pg_dsn() -> str:
    """Return an asyncpg DSN for a disposable PostgreSQL 16 Docker container.

    The fixture is opt-in because it starts a real container. Set
    ``JARVIS_RUN_LIVE_PG=1`` and run tests marked ``live_pg`` to exercise it.
    """
    if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
        pytest.skip("set JARVIS_RUN_LIVE_PG=1 to run Docker-backed live PostgreSQL tests")
    if shutil.which("docker") is None:
        pytest.fail("Docker CLI is required for JARVIS_RUN_LIVE_PG=1 live PostgreSQL tests")

    container = f"jarvis-le-live-pg-{uuid.uuid4().hex[:12]}"
    password = f"jarvis-test-{uuid.uuid4().hex}"
    image = os.environ.get("JARVIS_LIVE_PG_IMAGE", "postgres:16.8")
    _docker(
        [
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_DB=jarvis",
            "-e",
            "POSTGRES_USER=jarvis",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            "127.0.0.1::5432",
            image,
        ]
    )
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready = _docker(
                ["exec", container, "pg_isready", "-U", "jarvis", "-d", "jarvis"],
                check=False,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            logs = _docker(["logs", container], check=False, timeout=10)
            pytest.fail(f"PostgreSQL container did not become ready:\n{logs.stdout}{logs.stderr}")

        port_result = _docker(["port", container, "5432/tcp"])
        host_port = port_result.stdout.strip().rsplit(":", maxsplit=1)[-1]
        yield f"postgresql://jarvis:{password}@127.0.0.1:{host_port}/jarvis"
    finally:
        _docker(["rm", "-f", container], check=False, timeout=10)
