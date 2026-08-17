"""Shared database test infrastructure.

Provides asyncpg mock factories, live PostgreSQL fixtures, contract-database
adapters, a shared-connection pool, and focused row-seeding helpers. External
service boundaries keep their dedicated HTTP or SDK test doubles; the helpers
here model database interactions only.
"""

from __future__ import annotations

__all__ = [
    # Mock records, connections, pools, and requests
    "FakeRecord",
    "make_conn",
    "make_paper_record",
    "make_pool_and_conn",
    "_make_pool_and_conn",
    "make_multi_acquire_pool",
    "make_request",
    "shelve_paper",
    "seed_user_row",
    # Live PostgreSQL fixtures
    "make_live_pg_dsn",
    "make_live_pg_session_dsn",
    # Contract database fixtures
    "make_contract_pg_dsn",
    "_make_contract_pool_fixture",
    "_make_contract_conn_fixture",
    # Shared connection adapter
    "SharedAcquireCM",
    "SharedConnPool",
    # Cross-user contract seeds
    "TwoUsers",
    "_make_contract_two_users_fixture",
    "_seed_user",
    "_seed_resources",
    "A_PAPER_TITLE",
    "A_NOTE_TEXT",
    "A_PROJECT_NAME",
    "A_TASK_TITLE",
    "A_CARD_FRONT",
]

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import asyncpg
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
        """Return the value for *key*, or *default* when absent (dict.get contract)."""
        return super().get(key, default)


# ---------------------------------------------------------------------------
# Connection, record, and pool factories
# ---------------------------------------------------------------------------


def _wire_conn_returns(
    conn: AsyncMock,
    *,
    execute_return: Any = _UNSET,
    fetchval_return: Any = _UNSET,
    fetchrow_return: Any = _UNSET,
    fetch_return: Any = _UNSET,
) -> None:
    """Replace conn's query methods with AsyncMocks returning the given values."""
    if execute_return is not _UNSET:
        conn.execute = AsyncMock(return_value=execute_return)
    if fetchval_return is not _UNSET:
        conn.fetchval = AsyncMock(return_value=fetchval_return)
    if fetchrow_return is not _UNSET:
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    if fetch_return is not _UNSET:
        conn.fetch = AsyncMock(return_value=fetch_return)


def make_conn(
    *,
    execute_return: Any = _UNSET,
    fetchval_return: Any = _UNSET,
    fetchrow_return: Any = _UNSET,
    fetch_return: Any = _UNSET,
    with_transaction: bool = True,
) -> AsyncMock:
    """Return an asyncpg connection mock with explicitly configured results."""
    conn = AsyncMock(spec=asyncpg.Connection)

    _wire_conn_returns(
        conn,
        execute_return=execute_return,
        fetchval_return=fetchval_return,
        fetchrow_return=fetchrow_return,
        fetch_return=fetch_return,
    )
    if with_transaction:
        txn_cm = MagicMock()
        txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
        txn_cm.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=txn_cm)

    return conn


def make_paper_record(
    paper_id: int = 1,
    **overrides: Any,
) -> FakeRecord:
    """Return the shared asyncpg-record shape used by feed and dashboard tests."""
    discovered_at = overrides.pop("discovered_at", None)
    now = discovered_at or datetime.now(UTC)
    row: dict[str, Any] = {
        "id": paper_id,
        "external_id": f"arxiv:{paper_id}",
        "source_type": "arxiv",
        "title": f"Paper {paper_id}",
        "authors": ["Author A"],
        "abstract": "Abstract text",
        "published_date": None,
        "url": f"https://arxiv.org/abs/{paper_id}",
        "pdf_url": None,
        "pdf_local_path": None,
        "pdf_downloaded": False,
        "citation_count": 0,
        "metadata": {},
        "discovered_at": now,
        "created_at": now,
        "priority_score": None,
        "summary_brief": "Brief summary",
        "tldr": None,
        "confidence": "HIGH",
        "user_status": "new",
        "rating": None,
    }
    row.update(overrides)
    return FakeRecord(row)


def make_pool_and_conn(
    *,
    conn: AsyncMock | None = None,
    fetchval_return: Any = _UNSET,
    fetchrow_return: Any = _UNSET,
    fetch_return: Any = _UNSET,
    execute_return: Any = _UNSET,
    with_transaction: bool = True,
    raise_on_acquire: BaseException | None = None,
    fetchrow_side_effects: list | None = None,
    direct_methods: bool = False,
) -> tuple[MagicMock, AsyncMock]:
    """Return a ``(pool, conn)`` pair of asyncpg mocks.

    Keyword args wire canned return values and edge-case behaviours.
    ``direct_methods=True`` additionally exposes the conn's query methods
    (``fetch``/``fetchrow``/``fetchval``/``execute``/``executemany``) on the
    pool itself, for code that calls ``pool.fetchrow(...)`` without
    ``acquire()`` (mirrors ``SharedConnPool``); pool-level and conn-level
    calls then share one mock per method, so assertions see both.
    """
    if conn is None:
        conn = make_conn(with_transaction=with_transaction)
    elif with_transaction:
        txn_cm = MagicMock()
        txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
        txn_cm.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=txn_cm)

    _wire_conn_returns(
        conn,
        execute_return=execute_return,
        fetchval_return=fetchval_return,
        fetchrow_return=fetchrow_return,
        fetch_return=fetch_return,
    )

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    if raise_on_acquire is not None:
        pool.acquire = MagicMock(side_effect=raise_on_acquire)
    if fetchrow_side_effects is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effects)

    # Wired last so pool methods share the conn's FINAL mocks (a
    # fetchrow_side_effects list replaces conn.fetchrow above).
    if direct_methods:
        for method in ("fetch", "fetchrow", "fetchval", "execute", "executemany"):
            setattr(pool, method, getattr(conn, method))

    return pool, conn


def make_multi_acquire_pool(
    conns: int | list[AsyncMock],
    *,
    with_transaction: bool = True,
    await_acquire: bool = False,
) -> tuple[MagicMock, tuple[AsyncMock, ...]]:
    """Return ``(pool, conns)`` where successive acquires yield each conn in turn.

    ``conns`` is either a count (that many fresh ``make_conn`` mocks are built;
    ``with_transaction`` applies to them) or a list of pre-built connection
    mocks, used as-is. By default each ``pool.acquire()`` call returns an
    async-CM yielding the next conn; with ``await_acquire=True`` the code under
    test does ``conn = await pool.acquire()`` instead, and ``pool.release`` is
    an awaitable no-op. Acquiring more times than there are conns raises
    ``StopIteration`` — hand the factory exactly as many conns as the code
    under test acquires.
    """
    if isinstance(conns, int):
        conn_list = [make_conn(with_transaction=with_transaction) for _ in range(conns)]
    else:
        conn_list = list(conns)

    pool = MagicMock()
    if await_acquire:
        pool.acquire = AsyncMock(side_effect=conn_list)
        pool.release = AsyncMock()
    else:
        contexts = []
        for conn in conn_list:
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=conn)
            ctx.__aexit__ = AsyncMock(return_value=False)
            contexts.append(ctx)
        pool.acquire = MagicMock(side_effect=contexts)

    return pool, tuple(conn_list)


def make_request(user_id: int = 1, *, role: str | None = None, **state_overrides: Any) -> Any:
    """Minimal request mock; keyword args populate ``request.state`` for auth and handler tests."""
    from types import SimpleNamespace

    state = SimpleNamespace(user_id=user_id, **state_overrides)
    if role is not None:
        state.user_role = role
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace()))


async def shelve_paper(conn: Any, user_id: int, paper_id: int) -> None:
    """Add a paper to a user's library through the standard manual-save path."""
    await conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_id,
        paper_id,
    )


async def seed_user_row(conn: Any, email: str, role: str = "user") -> int:
    """Insert a user row without creating a session and return its identifier."""
    return int(
        await conn.fetchval(
            "INSERT INTO users (email, role) VALUES ($1, $2) RETURNING id",
            email,
            role,
        )
    )


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


def _docker_rm_best_effort(container: str) -> None:
    """Force-remove *container*, tolerating a momentarily slow/contended daemon.

    Teardown must never fail the test: on a loaded box ``docker rm -f`` can
    exceed its timeout (``subprocess`` raises ``TimeoutExpired``, which
    ``check=False`` does NOT suppress).  Retry once with backoff, then give up
    silently — the ``--rm`` flag already makes the container self-remove, so a
    leaked name is harmless and the next run's pre-clean handles it.
    """
    for attempt in range(2):
        try:
            _docker_cli(["rm", "-f", container], check=False, timeout=15)
            return
        except subprocess.TimeoutExpired:
            if attempt == 1:
                return
            time.sleep(1)


def _spin_pg_container(
    container_prefix: str,
    *,
    container_suffix: str,
    password_prefix: str,
) -> Iterator[str]:
    """Spin up a disposable postgres:16.8 container; yield its DSN; tear down on exit.

    FALLBACK-ONLY: used when ``JARVIS_TEST_PG_ADMIN_DSN`` is unset (local dev with
    no managed Postgres). CI and opt-in local runs take the managed-server path in
    ``_managed_or_spin`` and issue zero docker commands.

    Docker invariant: ``--rm`` means the container self-removes when stopped,
    but we still call ``docker rm -f`` in the finally block to ensure cleanup
    even if the container was not stopped cleanly (e.g. on SIGKILL).
    """
    container = f"{container_prefix}{container_suffix}-{uuid.uuid4().hex[:12]}"
    password = f"{password_prefix}-{uuid.uuid4().hex}"
    image = os.environ.get("JARVIS_LIVE_PG_IMAGE", "postgres:16.8")

    # Pre-cleanup: remove any zombie container from a prior run (e.g. CI job
    # killed between `docker run` and the `finally: docker rm -f` teardown).
    # Exit 125 = daemon refused to start because the name already exists.
    # `docker rm -f` is a no-op when the name is not found (exit 1, ignored).
    _docker_rm_best_effort(container)

    # Startup retry loop — two transient daemon faults under load are
    # tolerated, both with cleanup + backoff between attempts:
    #   * exit 125  — daemon refused: name still in use after pre-clean.
    #   * TimeoutExpired — `docker run` itself exceeded its deadline because the
    #     daemon was momentarily saturated (e.g. dozens of sequential live-PG
    #     containers in one job). A 60s timeout (vs the 30s default) absorbs a
    #     slow image-create; 3 attempts cover a brief contention spike.
    run_attempts = 3
    run_timeout = 60.0
    last_exc: BaseException | None = None
    for attempt in range(run_attempts):
        try:
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
                ],
                timeout=run_timeout,
            )
            last_exc = None
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            is_name_collision = (
                isinstance(exc, subprocess.CalledProcessError) and exc.returncode == 125
            )
            if not (is_name_collision or isinstance(exc, subprocess.TimeoutExpired)):
                raise
            if attempt == run_attempts - 1:
                raise
            last_exc = exc
            # Clean up any half-created container, then back off and retry.
            _docker_rm_best_effort(container)
            time.sleep(2)
    if last_exc is not None:
        raise last_exc
    try:
        # Bumped 45 → 90s deadline. pg_isready returns when the postmaster
        # accepts UNIX-socket connections; the TCP/SSL backend has a brief
        # follow-on window where asyncpg.create_pool can hit
        # ``ConnectionResetError [Errno 104] Connection reset by peer``. The
        # extended deadline + post-ready socket probe absorb that race.
        deadline = time.monotonic() + 90
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
            pytest.fail(f"PostgreSQL container did not become ready:\n{logs.stdout}{logs.stderr}")

        port_result = _docker_cli(["port", container, "5432/tcp"])
        host_port = port_result.stdout.strip().rsplit(":", maxsplit=1)[-1]

        # Socket probe — verify the TCP listener actually accepts a fresh
        # connection. pg_isready only confirms the postmaster is alive; the
        # network-facing TCP socket can lag by 100-500ms after that signal under
        # CI load.
        import socket as _socket

        socket_deadline = time.monotonic() + 30
        while time.monotonic() < socket_deadline:
            try:
                with _socket.create_connection(("127.0.0.1", int(host_port)), timeout=2):
                    break
            except (OSError, ConnectionResetError):
                time.sleep(0.25)
        else:
            logs = _docker_cli(["logs", container], check=False, timeout=10)
            pytest.fail(
                f"PostgreSQL container ready per pg_isready but TCP socket {host_port} "
                f"did not accept connections within 30s:\n{logs.stdout}{logs.stderr}"
            )

        # CI-E: protocol-level probe — the TCP socket accepting a connection does
        # NOT guarantee the PostgreSQL startup-packet handshake is ready.  asyncpg
        # receives ConnectionResetError [Errno 104] when PG resets the connection
        # mid-handshake (backend not yet initialised).  The TCP probe only checks
        # the OS-level accept(); we must verify the actual PG protocol is ready
        # before yielding the DSN so that test-body asyncpg.create_pool() calls
        # (which carry zero retry) cannot race this window.
        #
        # `psql -c "SELECT 1"` runs inside the container (no host asyncpg import
        # needed in a sync generator context) and exits 0 only when PG has
        # completed its startup and accepts a full query round-trip.
        proto_deadline = time.monotonic() + 30
        while time.monotonic() < proto_deadline:
            proto = _docker_cli(
                [
                    "exec",
                    container,
                    "psql",
                    "-U",
                    "jarvis",
                    "-d",
                    "jarvis",
                    "-c",
                    "SELECT 1",
                ],
                check=False,
                timeout=5,
            )
            if proto.returncode == 0:
                break
            time.sleep(0.25)
        else:
            logs = _docker_cli(["logs", container], check=False, timeout=10)
            pytest.fail(
                f"PostgreSQL container TCP-ready on {host_port} but protocol "
                f"(psql SELECT 1) did not succeed within 30s:\n"
                f"{logs.stdout}{logs.stderr}"
            )

        yield f"postgresql://jarvis:{quote(password, safe='')}@127.0.0.1:{host_port}/jarvis"
    finally:
        _docker_rm_best_effort(container)


# ---------------------------------------------------------------------------
# Managed-Postgres path (JARVIS_TEST_PG_ADMIN_DSN set)
# ---------------------------------------------------------------------------
# When a long-lived admin Postgres is available (the CI ``services:`` server, or
# an opt-in local one), each fixture gets a fresh EMPTY database via
# ``CREATE DATABASE … TEMPLATE template0`` instead of spinning a container. This
# removes ``docker run`` from pytest entirely (eliminating docker-daemon contention
# as a source of DB-test flakes) — so parallel runners stop contending on the Docker
# daemon. The db is empty, so the consumer pool applies db/init.sql +
# run_migrations exactly as it did against a throwaway container: transparent.
# Unset the env var (local dev, no managed PG) → fall back to _spin_pg_container.


def _admin_dsn() -> str | None:
    """Admin DSN for the managed test Postgres, or None to fall back to
    per-fixture throwaway containers (local dev)."""
    return os.environ.get("JARVIS_TEST_PG_ADMIN_DSN")


def _with_dbname(dsn: str, dbname: str) -> str:
    """Return *dsn* with its database path component replaced by *dbname*."""
    from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

    return urlunsplit(urlsplit(dsn)._replace(path=f"/{dbname}"))


def _run_sync(coro: Any) -> Any:
    """Drive *coro* to completion in a private event loop.

    The DSN factories are sync pytest fixtures, but the admin CREATE/DROP
    DATABASE calls are async (asyncpg). A throwaway loop keeps these brief admin
    ops isolated from pytest-asyncio's session loop.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _admin_connect(admin_dsn: str) -> asyncpg.Connection:
    """Connect to the admin server's maintenance db, retrying briefly while a
    freshly-started Postgres finishes accepting connections.

    The CI ``services:`` health-gate usually makes this a single attempt; the
    opt-in local admin path has no gate, so 3 attempts absorb a cold start
    (mirrors ``_spin_pg_container``'s 3-attempt run loop).
    """
    import asyncpg  # noqa: PLC0415

    conn = None
    for attempt in range(3):
        try:
            conn = await asyncpg.connect(admin_dsn)
            break
        except (OSError, asyncpg.PostgresError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.5)
    assert conn is not None
    return conn


async def _create_fresh_db(admin_dsn: str, prefix: str) -> tuple[str, str]:
    """``CREATE DATABASE "<prefix>_<uuid12>" TEMPLATE template0`` on the admin
    server; return ``(dsn_to_new_db, dbname)``.

    template0 is the pristine, always-copyable template (no active connections
    to copy). CREATE DATABASE cannot run inside a transaction; asyncpg
    connections are autocommit unless a transaction is opened, so a bare
    ``execute`` is correct. The CREATE is retried on a transient PostgresError
    (e.g. a momentary template lock if two jobs ever share one admin server).
    The new db is EMPTY — the consumer pool applies the schema, exactly as for a
    container. ``hex[:12]`` keeps the identifier well under Postgres' 63-byte
    limit even with the longest service prefix.
    """
    import asyncpg  # noqa: PLC0415

    dbname = f"{prefix.replace('-', '_')}_{uuid.uuid4().hex[:12]}"
    assert len(dbname.encode()) <= 63, f"db identifier too long: {dbname!r}"
    conn = await _admin_connect(admin_dsn)
    try:
        for attempt in range(3):
            try:
                await conn.execute(f'CREATE DATABASE "{dbname}" TEMPLATE template0')
                break
            except asyncpg.PostgresError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5)
    finally:
        await conn.close()
    return _with_dbname(admin_dsn, dbname), dbname


async def _drop_db(admin_dsn: str, dbname: str) -> None:
    """``DROP DATABASE IF EXISTS <dbname> WITH (FORCE)`` — drop the per-fixture
    database at teardown, terminating any app-pool backends still attached.

    Uses the same retrying connect as create so a transient blip at teardown
    doesn't error an otherwise-green run.
    """
    conn = await _admin_connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    finally:
        await conn.close()


def _managed_or_spin(
    container_prefix: str,
    *,
    admin_suffix: str,
    container_suffix: str,
    password_prefix: str,
) -> Iterator[str]:
    """Yield a live-PG DSN: a fresh empty db on the managed server when
    ``JARVIS_TEST_PG_ADMIN_DSN`` is set (zero docker commands), else a throwaway
    container (today's local-dev path). Shared body of all 3 DSN factories.
    """
    admin = _admin_dsn()
    if admin is not None:
        dsn, dbname = _run_sync(_create_fresh_db(admin, f"{container_prefix}{admin_suffix}"))
        try:
            yield dsn
        finally:
            _run_sync(_drop_db(admin, dbname))
    else:
        if shutil.which("docker") is None:
            pytest.fail("Docker CLI is required for JARVIS_RUN_LIVE_PG=1 live PostgreSQL tests")
        yield from _spin_pg_container(
            container_prefix,
            container_suffix=container_suffix,
            password_prefix=password_prefix,
        )


def make_live_pg_dsn(container_prefix: str):  # -> pytest fixture
    """Return a ``live_pg_dsn`` pytest fixture scoped to *container_prefix*.

    Each service passes its own prefix (``jarvis-rd``, ``jarvis-le``, etc.)
    so that parallel runs can spin up independent containers without name
    collisions.  The returned object is a generator function decorated with
    ``@pytest.fixture()``.
    """

    @pytest.fixture()
    def live_pg_dsn() -> Iterator[str]:
        """Return an asyncpg DSN for a disposable PostgreSQL 16 Docker container.

        The fixture is opt-in because it starts a real container. Set
        ``JARVIS_RUN_LIVE_PG=1`` and run tests marked ``live_pg`` to exercise it.
        """
        if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
            pytest.skip("set JARVIS_RUN_LIVE_PG=1 to run Docker-backed live PostgreSQL tests")

        yield from _managed_or_spin(
            container_prefix,
            admin_suffix="_live",
            container_suffix="-live-pg",
            password_prefix="jarvis-test",
        )

    return live_pg_dsn


def make_live_pg_session_dsn(container_prefix: str):  # -> pytest fixture (session scope)
    """Return a SESSION-scoped live-PG ``xuser_pg_dsn`` fixture for *container_prefix*.

    Unlike ``make_live_pg_dsn`` (function scope — one container per test), this
    spawns ONE postgres:16.8 container for the whole session (suffix ``-xuser``).
    The cross-user isolation suite uses it so its ~53 parametrized cases share a
    single container with per-test TRUNCATE+reseed instead of 53 throwaway
    containers (avoids docker-daemon saturation on loaded CI runners). The
    ``-xuser`` suffix keeps it independent of the
    contract suite's ``-contract`` session container so lifecycles never contend.
    """

    @pytest.fixture(scope="session")
    def xuser_pg_dsn() -> Iterator[str]:
        if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
            pytest.skip(
                "set JARVIS_RUN_LIVE_PG=1 to run the cross-user isolation suite "
                "(DB-backed; session-scoped postgres container)"
            )

        yield from _managed_or_spin(
            container_prefix,
            admin_suffix="_xuser",
            container_suffix="-xuser",
            password_prefix="jarvis-xuser",
        )

    return xuser_pg_dsn


# ---------------------------------------------------------------------------
# Contract-layer fixture factories
# ---------------------------------------------------------------------------


def make_contract_pg_dsn(container_prefix: str):  # -> pytest fixture (session scope)
    """Return a session-scoped contract_pg_dsn pytest fixture for *container_prefix*.

    Spawns ONE postgres:16.8 container per pytest session, applies the
    canonical schema (db/init.sql) + run_migrations() once, yields the DSN
    for the lifetime of the session. The per-test rollback isolation is
    provided by the function-scoped contract_conn fixture below.
    """

    @pytest.fixture(scope="session")
    def contract_pg_dsn() -> Iterator[str]:
        if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
            pytest.skip(
                "set JARVIS_RUN_LIVE_PG=1 to run contract-layer tests "
                "(DB-backed; session-scoped postgres container)"
            )

        yield from _managed_or_spin(
            container_prefix,
            admin_suffix="_contract",
            container_suffix="-contract",
            password_prefix="jarvis-contract",
        )

    return contract_pg_dsn


def _make_contract_pool_fixture():
    """Return a session-scoped fixture providing an asyncpg pool against
    contract_pg_dsn. Applies db/init.sql + run_migrations() once per session.
    """
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="session", loop_scope="session")
    async def _contract_pool(contract_pg_dsn: str):
        import asyncio
        from pathlib import Path

        import asyncpg

        from jarvis_common.db_helpers import init_pg_connection  # noqa: PLC0415
        from jarvis_common.migrations import run_migrations  # noqa: PLC0415

        db_dir = Path(__file__).resolve().parents[3] / "db"
        init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
        migrations_dir = db_dir / "migrations"
        contract_search_path = "platform, research, learning, ops, public, pg_catalog"

        async def setup_contract_connection(conn: asyncpg.Connection) -> None:
            """Restore the cross-domain path whenever the pool lends a connection."""
            await conn.execute(f"SET search_path TO {contract_search_path}")

        pool = None
        for attempt in range(10):
            try:
                pool = await asyncpg.create_pool(
                    contract_pg_dsn,
                    min_size=1,
                    max_size=5,
                    init=init_pg_connection,
                    setup=setup_contract_connection,
                )
                break
            except (OSError, asyncpg.PostgresError):
                if attempt == 9:
                    raise
                await asyncio.sleep(0.5)
        assert pool is not None
        try:
            async with pool.acquire() as conn:
                await conn.execute(init_sql)
            await run_migrations(pool, migrations_dir=migrations_dir)
            yield pool
        finally:
            await pool.close()

    return _contract_pool


def _make_contract_conn_fixture():
    """Return a function-scoped fixture wrapping each test in a transaction
    rolled back at teardown. asyncpg's savepoint semantics let app code's own
    conn.transaction() nest correctly inside this outer transaction.
    """
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="function", loop_scope="session")
    async def contract_conn(_contract_pool):
        async with _contract_pool.acquire() as conn:
            txn = conn.transaction()
            await txn.start()
            try:
                yield conn
            finally:
                await txn.rollback()

    return contract_conn


# ---------------------------------------------------------------------------
# SharedConnPool / SharedAcquireCM
# ---------------------------------------------------------------------------


class _TaskReentrantAsyncLock:
    """Serialize shared asyncpg access while allowing same-task nested acquires."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and task is self._owner:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        task = asyncio.current_task()
        if task is not self._owner:
            raise RuntimeError("SharedConnPool lock released by non-owner task")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


class SharedAcquireCM:
    """Async CM returned by SharedConnPool.acquire(); always yields the same conn."""

    def __init__(
        self,
        conn: Any,
        lock: _TaskReentrantAsyncLock,
        session_authorization: str | None,
    ) -> None:
        """Hold the shared connection and the reentrant lock used to serialize access."""
        self._conn = conn
        self._lock = lock
        self._session_authorization = session_authorization
        self._owns_authorization = False

    async def __aenter__(self) -> Any:
        await self._lock.acquire()
        if self._session_authorization is not None and self._lock._depth == 1:
            await self._conn.execute(
                f"SET LOCAL SESSION AUTHORIZATION {self._session_authorization}"
            )
            self._owns_authorization = True
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        try:
            if self._owns_authorization:
                await self._conn.execute("RESET SESSION AUTHORIZATION")
        finally:
            self._lock.release()
        return None


class SharedConnPool:
    """Pool-shaped object that always returns a single shared connection from acquire().

    Lets a service's FastAPI app see the SAME asyncpg connection (and therefore
    the same outer transaction) as the test's contract_conn fixture, so writes
    from both ends share one rollback boundary.

    Mirrors asyncpg.Pool's convenience methods (``fetch``/``fetchrow``/``fetchval``/
    ``execute``/``executemany``) by delegating to the shared connection, so service
    code that calls ``db_pool.fetchrow(...)`` directly (without ``acquire()``) works
    against the contract DB.
    """

    def __init__(
        self,
        conn: Any,
        *,
        session_authorization: str | None = None,
        _lock: _TaskReentrantAsyncLock | None = None,
    ) -> None:
        """Wrap *conn* with serialized access and an optional runtime identity.

        ``session_authorization`` is restricted to the three product runtime
        roles. Contract applications use it so capability checks observe the
        same login identity as production while direct fixture assertions
        regain the administrative test identity after each pool operation.
        """
        allowed_roles = {
            "jarvis_platform_runtime",
            "jarvis_research_runtime",
            "jarvis_learning_runtime",
        }
        if session_authorization is not None and session_authorization not in allowed_roles:
            raise ValueError("unsupported contract runtime identity")
        self._conn = conn
        self._lock = _lock or _TaskReentrantAsyncLock()
        self._session_authorization = session_authorization

    def with_session_authorization(self, role: str) -> SharedConnPool:
        """Return a role-specific view sharing this connection's access lock.

        Parameters
        ----------
        role : str
            Supported product runtime role applied for each acquired operation.

        Returns
        -------
        SharedConnPool
            Pool-shaped view that serializes against every sibling view of the
            same contract connection.
        """
        return SharedConnPool(
            self._conn,
            session_authorization=role,
            _lock=self._lock,
        )

    def acquire(self) -> SharedAcquireCM:  # not async — returns an async-CM
        """Return an async CM that yields the shared connection under the reentrant lock."""
        return SharedAcquireCM(
            self._conn,
            self._lock,
            self._session_authorization,
        )

    async def close(self) -> None:  # idempotent; the real pool's lifecycle is the fixture's
        """No-op — the underlying connection is managed by the fixture, not this pool shim."""
        return None

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetch(*args, **kwargs)

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchrow(*args, **kwargs)

    async def fetchval(self, *args: Any, **kwargs: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(*args, **kwargs)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.execute(*args, **kwargs)

    async def executemany(self, *args: Any, **kwargs: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.executemany(*args, **kwargs)


# ---------------------------------------------------------------------------
# Seed helpers (moved from paper_ingestion conftest, D5)
# ---------------------------------------------------------------------------


class TwoUsers:
    """Handle exposing two real DB users plus their seeded, owned resources.

    Every ``*_a`` attribute is owned by ``user_a_id``; the negative test acts
    as user B (``cookie_b``) and asserts it can neither read nor mutate any
    of A's rows. ``cookie_*`` are ready-to-use ``jarvis_session`` cookie
    values (the session row's UUID id).
    """

    def __init__(self, **kw: object) -> None:
        """Populate all attributes from keyword arguments (used by the fixture factory)."""
        self.__dict__.update(kw)

    user_a_id: int
    user_b_id: int
    cookie_a: str
    cookie_b: str
    paper_id_a: int
    note_id_a: int
    card_id_a: int
    deck_id_a: int
    project_id_a: int
    project_id_b: int
    task_id_a: int
    journal_id_a: int
    topic_id_a: int
    pulse_deck_id_a: int
    pulse_card_id_a: int
    pool: object  # asyncpg.Pool — live schema, used for app wiring + re-checks


# Marker strings the test asserts are NEVER visible to user B.
A_PAPER_TITLE = "ZZZ-ISOLATION-A-PAPER Quantum Entanglement of Owls"
A_NOTE_TEXT = "ZZZ-ISOLATION-A-NOTE private annotation alpha"
A_PROJECT_NAME = "ZZZ-ISOLATION-A-PROJECT secret roadmap"
A_TASK_TITLE = "ZZZ-ISOLATION-A-TASK confidential milestone"
A_CARD_FRONT = "ZZZ-ISOLATION-A-CARD front side alpha"


async def _seed_user(conn, email: str) -> tuple[int, str]:
    """Insert one active user + one valid session; return (user_id, cookie)."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        email,
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day')
           RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


async def _seed_resources(conn, user_id: int, tag: str) -> dict:
    """Seed one owned row per DB-backed table the endpoints read."""
    paper_id = await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', $2, ARRAY['A. Author'], 'https://example.test/a', $3)
           RETURNING id""",
        f"iso-ext-{tag}",
        A_PAPER_TITLE if tag == "a" else f"paper-{tag}",
        user_id,
    )
    await conn.execute(
        """INSERT INTO user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        user_id,
        paper_id,
    )
    await conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, starred)
           VALUES ($1, $2, 'to_read', TRUE)""",
        paper_id,
        user_id,
    )
    note_id = await conn.fetchval(
        """INSERT INTO paper_notes (paper_id, user_note, user_id)
           VALUES ($1, $2, $3) RETURNING id""",
        paper_id,
        A_NOTE_TEXT if tag == "a" else f"note-{tag}",
        user_id,
    )
    deck_id = await conn.fetchval(
        "INSERT INTO decks (name, user_id) VALUES ($1, $2) RETURNING id",
        f"deck-{tag}",
        user_id,
    )
    card_id = await conn.fetchval(
        """INSERT INTO cards (deck_id, paper_id, card_type, front, back, user_id)
           VALUES ($1, $2, 'concept', $3, 'back', $4) RETURNING id""",
        deck_id,
        paper_id,
        A_CARD_FRONT if tag == "a" else f"card-{tag}",
        user_id,
    )
    project_id = await conn.fetchval(
        """INSERT INTO projects (name, user_id) VALUES ($1, $2) RETURNING id""",
        A_PROJECT_NAME if tag == "a" else f"project-{tag}",
        user_id,
    )
    task_id = await conn.fetchval(
        """INSERT INTO tasks (project_id, title, user_id)
           VALUES ($1, $2, $3) RETURNING id""",
        project_id,
        A_TASK_TITLE if tag == "a" else f"task-{tag}",
        user_id,
    )
    journal_id = await conn.fetchval(
        """INSERT INTO journal_entries (user_id, date, prompts)
           VALUES ($1, CURRENT_DATE, '{"win": "secret"}'::jsonb)
           RETURNING id""",
        user_id,
    )
    topic_id = await conn.fetchval(
        """INSERT INTO topics (name, query_terms) VALUES ($1, ARRAY['q'])
           RETURNING id""",
        f"topic-{tag}",
    )
    await conn.execute(
        """INSERT INTO user_topic_subscriptions (user_id, topic_id)
           VALUES ($1, $2)""",
        user_id,
        topic_id,
    )
    await conn.execute(
        """INSERT INTO paper_recommendations (paper_id, score, user_id)
           VALUES ($1, 0.9, $2)""",
        paper_id,
        user_id,
    )
    pulse_deck_id = await conn.fetchval(
        """INSERT INTO pulse_decks (deck_date, card_count, user_id)
           VALUES (CURRENT_DATE, 1, $1) RETURNING id""",
        user_id,
    )
    pulse_card_id = await conn.fetchval(
        """INSERT INTO pulse_cards (deck_id, paper_id, rank, score, user_id)
           VALUES ($1, $2, 1, 0.9, $3) RETURNING id""",
        pulse_deck_id,
        paper_id,
        user_id,
    )
    return {
        "paper_id": paper_id,
        "note_id": note_id,
        "card_id": card_id,
        "deck_id": deck_id,
        "project_id": project_id,
        "task_id": task_id,
        "journal_id": journal_id,
        "topic_id": topic_id,
        "pulse_deck_id": pulse_deck_id,
        "pulse_card_id": pulse_card_id,
    }


def _make_contract_two_users_fixture():
    """Return a function-scoped fixture seeding two users + resources under the
    contract_conn transaction so the seed is contained by per-test rollback.
    """
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="function", loop_scope="session")
    async def contract_two_users(contract_conn) -> TwoUsers:
        user_a_id, cookie_a = await _seed_user(contract_conn, "iso-user-a@contract.example.com")
        user_b_id, cookie_b = await _seed_user(contract_conn, "iso-user-b@contract.example.com")
        res_a = await _seed_resources(contract_conn, user_a_id, "a")
        res_b = await _seed_resources(contract_conn, user_b_id, "b")
        return TwoUsers(
            user_a_id=user_a_id,
            user_b_id=user_b_id,
            cookie_a=cookie_a,
            cookie_b=cookie_b,
            paper_id_a=res_a["paper_id"],
            note_id_a=res_a["note_id"],
            card_id_a=res_a["card_id"],
            deck_id_a=res_a["deck_id"],
            project_id_a=res_a["project_id"],
            project_id_b=res_b["project_id"],
            task_id_a=res_a["task_id"],
            journal_id_a=res_a["journal_id"],
            topic_id_a=res_a["topic_id"],
            pulse_deck_id_a=res_a["pulse_deck_id"],
            pulse_card_id_a=res_a["pulse_card_id"],
            pool=None,  # contract layer uses contract_conn directly
        )

    return contract_two_users
