"""Canonical shared test-infrastructure for all JARVIS services.

This module is the single source of truth for cross-service test helpers.
Each service's ``tests/conftest.py`` re-exports from here so that the
``--import-mode=importlib`` + shared ``tests`` namespace invariant (see
pyproject.toml §tool.pytest.ini_options) stays intact: every
``from tests.conftest import <symbol>`` resolves to the same object
regardless of which service's conftest wins the namespace race.

Public API
----------
FakeRecord              asyncpg.Record dict-shim (attr + .get access)
make_pool_and_conn      canonical mock (pool, conn) factory with optional kwargs
_make_pool_and_conn     module-level alias preserved for the 76 existing importers
make_live_pg_dsn        factory that returns a ``live_pg_dsn`` pytest fixture
RoleMiddleware          ASGI middleware shim that injects request.state.user_role
FakeAcquireCM           async CM returned by pool.acquire()
FakeTxnCM               async CM returned by conn.transaction()
make_telegram_update    build a minimal PTB Update-like MagicMock
make_bot_config         build a minimal BotConfig for telegram_bot tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Sentinel used to distinguish "not passed" from "explicitly None"
# ---------------------------------------------------------------------------

_UNSET: Any = object()


# ---------------------------------------------------------------------------
# FakeRecord
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Unified asyncpg.Record substitute: dict[], .attr, .keys(), .get(), .values()."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return super().get(key, default)


# ---------------------------------------------------------------------------
# make_pool_and_conn
# ---------------------------------------------------------------------------


def make_pool_and_conn(
    *,
    conn: AsyncMock | None = None,
    fetchval_return: Any = _UNSET,
    fetchrow_return: Any = _UNSET,
    fetch_return: Any = _UNSET,
    with_transaction: bool = True,
) -> tuple[MagicMock, AsyncMock]:
    """Return a ``(pool, conn)`` pair of asyncpg mock objects.

    Parameters
    ----------
    conn:
        Pass an existing AsyncMock connection to wrap instead of building a
        fresh one. Useful for tests that pre-configure conn behaviour before
        calling into service code.
    fetchval_return:
        If provided (including ``None``), sets ``conn.fetchval.return_value``.
    fetchrow_return:
        If provided (including ``None``), sets ``conn.fetchrow.return_value``.
    fetch_return:
        If provided (including ``None``), sets ``conn.fetch.return_value``.
    with_transaction:
        When ``True`` (default) wires ``conn.transaction`` to a working async
        context manager. Set ``False`` only for the rare helpers that test
        paths that explicitly skip transactions.
    """
    if conn is None:
        conn = AsyncMock()

    if with_transaction:
        txn_cm = MagicMock()
        txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
        txn_cm.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=txn_cm)

    if fetchval_return is not _UNSET:
        conn.fetchval = AsyncMock(return_value=fetchval_return)
    if fetchrow_return is not _UNSET:
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    if fetch_return is not _UNSET:
        conn.fetch = AsyncMock(return_value=fetch_return)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


# Module-level alias: the 76 importers use ``_make_pool_and_conn``; the
# pyproject.toml comment explains why the name must stay stable here.
_make_pool_and_conn = make_pool_and_conn


# ---------------------------------------------------------------------------
# Live-PostgreSQL fixture factory
# ---------------------------------------------------------------------------


def _docker_cli(
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


def make_live_pg_dsn(container_prefix: str):  # -> pytest fixture
    """Return a ``live_pg_dsn`` pytest fixture scoped to *container_prefix*.

    Each service passes its own prefix (``jarvis-rd``, ``jarvis-le``, etc.)
    so that parallel runs can spin up independent containers without name
    collisions.  The returned object is a generator function decorated with
    ``@pytest.fixture()``.
    """

    @pytest.fixture()
    def live_pg_dsn() -> str:  # type: ignore[return]
        """Return an asyncpg DSN for a disposable PostgreSQL 16 Docker container.

        The fixture is opt-in because it starts a real container. Set
        ``JARVIS_RUN_LIVE_PG=1`` and run tests marked ``live_pg`` to exercise it.
        """
        if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
            pytest.skip("set JARVIS_RUN_LIVE_PG=1 to run Docker-backed live PostgreSQL tests")
        if shutil.which("docker") is None:
            pytest.fail("Docker CLI is required for JARVIS_RUN_LIVE_PG=1 live PostgreSQL tests")

        container = f"{container_prefix}-live-pg-{uuid.uuid4().hex[:12]}"
        password = f"jarvis-test-{uuid.uuid4().hex}"
        image = os.environ.get("JARVIS_LIVE_PG_IMAGE", "postgres:16.8")
        _docker_cli(
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
                ready = _docker_cli(
                    ["exec", container, "pg_isready", "-U", "jarvis", "-d", "jarvis"],
                    check=False,
                    timeout=5,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                logs = _docker_cli(["logs", container], check=False, timeout=10)
                pytest.fail(
                    f"PostgreSQL container did not become ready:\n{logs.stdout}{logs.stderr}"
                )

            port_result = _docker_cli(["port", container, "5432/tcp"])
            host_port = port_result.stdout.strip().rsplit(":", maxsplit=1)[-1]
            yield f"postgresql://jarvis:{password}@127.0.0.1:{host_port}/jarvis"
        finally:
            _docker_cli(["rm", "-f", container], check=False, timeout=10)

    return live_pg_dsn


# ---------------------------------------------------------------------------
# RoleMiddleware
# ---------------------------------------------------------------------------


class RoleMiddleware:
    """Minimal ASGI middleware that injects request.state.user_role before routing.

    When ``role`` is ``None`` the attribute is deliberately left absent,
    which exercises the API-key path in admin-gate tests.
    """

    def __init__(self, app: Any, role: str | None) -> None:
        self._app = app
        self._role = role

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and self._role is not None:
            from starlette.requests import Request

            request = Request(scope)
            request.state.user_role = self._role
        await self._app(scope, receive, send)


# ---------------------------------------------------------------------------
# FakeAcquireCM / FakeTxnCM  (telegram_bot pairing tests, D8-04)
# ---------------------------------------------------------------------------


class FakeAcquireCM:
    """Async context manager returned by ``pool.acquire()`` in telegram tests."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeTxnCM:
    """Async context manager returned by ``conn.transaction()`` in telegram tests."""

    async def __aenter__(self) -> FakeTxnCM:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Telegram helpers  (D9-04 — _make_update defined 6× across telegram_bot)
# ---------------------------------------------------------------------------


def make_telegram_update(
    chat_id: int = 42,
    *,
    text: str | None = None,
    user_id: int | None = None,
    username: str | None = "testuser",
) -> MagicMock:
    """Build a minimal PTB ``Update``-like MagicMock.

    Superset of all 6 local ``_make_update`` variants found in
    ``telegram_bot/tests/``:

    - ``test_pairing.py``         — chat_id, text, username
    - ``test_pairing_command.py`` — chat_id, text
    - ``test_pairing_takeover.py``— chat_id
    - ``test_rate_limit.py``      — chat_id
    - ``test_auth.py``            — chat_id
    - ``test_dispatcher_correlation.py`` — chat_id

    ``user_id`` wires ``update.effective_user.id`` for handlers that inspect
    the PTB user object (not used in current tests but anticipated by D9-04).
    ``username`` wires ``update.effective_chat.username``.
    """
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.username = username
    update.message = MagicMock()
    if text is not None:
        update.message.text = text
    update.message.reply_text = AsyncMock()
    if user_id is not None:
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
    return update


def make_bot_config(**overrides: Any) -> Any:
    """Build a minimal ``BotConfig`` for telegram_bot unit tests.

    Defaults match the canonical ``_make_config`` in ``test_pairing.py``;
    pass ``**overrides`` to change individual fields (e.g.
    ``telegram_chat_id=None`` for pairing-flow tests).

    Import is deferred so that ``jarvis_common.testing`` can be imported by
    all services — even those that don't have ``telegram_bot`` on sys.path.
    The call will fail with an ImportError only if telegram_bot is absent
    AND the caller actually invokes this function.
    """
    from pydantic import SecretStr
    from telegram_bot.config import BotConfig

    defaults: dict[str, Any] = dict(
        telegram_token="test-token",
        telegram_chat_id=777,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key=SecretStr("test-key"),
    )
    defaults.update(overrides)
    return BotConfig(**defaults)
