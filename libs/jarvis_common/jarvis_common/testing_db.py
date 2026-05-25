"""Shared DB test infrastructure.

Asyncpg mocks, live-PG fixtures, contract pool/conn fixtures, shared-conn
pool, and TwoUsers seed helpers — clusters 1-5 of the 2026-05-24 polish-wave
decomposition of ``jarvis_common.testing``:

1. FakeRecord + ``make_pool_and_conn`` + ``make_request`` (mock asyncpg pool/conn factories)
2. LivePG fixture (``_docker_cli`` + ``make_live_pg_dsn``)
3. Contract fixture factories (``make_contract_pg_dsn``, ``_make_contract_pool_fixture``,
   ``_make_contract_conn_fixture``)
4. ``SharedConnPool`` + reentrant async lock
5. ``TwoUsers`` seed + marker constants + ``_seed_user`` / ``_seed_resources`` /
   ``_make_contract_two_users_fixture``

Idiomatic-mock carve-out: the following external boundaries MUST keep
AsyncMock / @patch and must NOT be collapsed into contract_conn:
  - Ollama embed (httpx call in embed_texts)
  - Qdrant query (qdrant_client)
  - Telegram Bot API (python-telegram-bot Application)
  - OpenAI / Instructor (openai.AsyncOpenAI)
  - Langfuse trace/span (langfuse SDK)
  - task_registry._TASK_MAP (module-level mutable dict)
These boundaries own their own I/O contract; only asyncpg pool mocks
collapse into contract_conn. This carve-out registry is the load-bearing
rule for Sub-wave 4.4 test migrations.
"""

from __future__ import annotations

__all__ = [
    # cluster 1
    "FakeRecord",
    "make_pool_and_conn",
    "_make_pool_and_conn",
    "make_request",
    # cluster 2
    "make_live_pg_dsn",
    # cluster 3
    "make_contract_pg_dsn",
    "_make_contract_pool_fixture",
    "_make_contract_conn_fixture",
    # cluster 4
    "SharedAcquireCM",
    "SharedConnPool",
    # cluster 5
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
        """Return the value for *key*, or *default* when absent (dict.get contract)."""
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
    raise_on_acquire: BaseException | None = None,
    fetchrow_side_effects: list | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """Return a ``(pool, conn)`` pair of asyncpg mocks.

    Keyword args wire canned return values and edge-case behaviours.
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

    if raise_on_acquire is not None:
        pool.acquire = MagicMock(side_effect=raise_on_acquire)
    if fetchrow_side_effects is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effects)

    return pool, conn


def make_request(user_id: int = 1, *, role: str | None = None, **state_overrides: Any) -> Any:
    """Minimal request mock; keyword args populate ``request.state`` for auth and handler tests."""
    from types import SimpleNamespace

    state = SimpleNamespace(user_id=user_id, **state_overrides)
    if role is not None:
        state.user_role = role
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace()))


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


def _spin_pg_container(
    container_prefix: str,
    *,
    container_suffix: str,
    password_prefix: str,
) -> Iterator[str]:
    """Spin up a disposable postgres:16.8 container; yield its DSN; tear down on exit.

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
    _docker_cli(["rm", "-f", container], check=False, timeout=10)

    # Retry loop: handles transient daemon refusals (exit 125) after pre-clean.
    # Two attempts with 1s backoff is sufficient for the CI name-collision flake
    # without adding a general retry framework.
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(2):
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
                ]
            )
            last_exc = None
            break
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 125 or attempt == 1:
                raise
            last_exc = exc
            # Force-remove the conflicting container then retry once.
            _docker_cli(["rm", "-f", container], check=False, timeout=10)
            time.sleep(1)
    if last_exc is not None:
        raise last_exc
    try:
        # W6-01: bumped 45 → 90s deadline. pg_isready returns when the postmaster
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

        # W6-01: socket probe — verify the TCP listener actually accepts a fresh
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

        yield f"postgresql://jarvis:{password}@127.0.0.1:{host_port}/jarvis"
    finally:
        _docker_cli(["rm", "-f", container], check=False, timeout=10)


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
        if shutil.which("docker") is None:
            pytest.fail("Docker CLI is required for JARVIS_RUN_LIVE_PG=1 live PostgreSQL tests")

        yield from _spin_pg_container(
            container_prefix,
            container_suffix="-live-pg",
            password_prefix="jarvis-test",
        )

    return live_pg_dsn


# ---------------------------------------------------------------------------
# Contract-layer fixture factories (Wave 4)
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
        if shutil.which("docker") is None:
            pytest.fail("Docker CLI is required for contract-layer tests")

        yield from _spin_pg_container(
            container_prefix,
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

        pool = None
        for attempt in range(10):
            try:
                pool = await asyncpg.create_pool(
                    contract_pg_dsn,
                    min_size=1,
                    max_size=5,
                    init=init_pg_connection,
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

    def __init__(self, conn: Any, lock: _TaskReentrantAsyncLock) -> None:
        """Hold the shared connection and the reentrant lock used to serialize access."""
        self._conn = conn
        self._lock = lock

    async def __aenter__(self) -> Any:
        await self._lock.acquire()
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
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

    def __init__(self, conn: Any) -> None:
        """Wrap *conn* with a reentrant lock so all pool-shaped calls share one connection."""
        self._conn = conn
        self._lock = _TaskReentrantAsyncLock()

    def acquire(self) -> SharedAcquireCM:  # not async — returns an async-CM
        """Return an async CM that yields the shared connection under the reentrant lock."""
        return SharedAcquireCM(self._conn, self._lock)

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
        await _seed_resources(contract_conn, user_b_id, "b")
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
            task_id_a=res_a["task_id"],
            journal_id_a=res_a["journal_id"],
            topic_id_a=res_a["topic_id"],
            pulse_deck_id_a=res_a["pulse_deck_id"],
            pulse_card_id_a=res_a["pulse_card_id"],
            pool=None,  # contract layer uses contract_conn directly
        )

    return contract_two_users
