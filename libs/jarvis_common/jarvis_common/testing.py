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
    raise_on_acquire: BaseException | None = None,
    fetchrow_side_effects: list | None = None,
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
    raise_on_acquire:
        When set, ``pool.acquire()`` raises this exception instead of yielding
        a connection. Useful for testing error-handling paths around pool
        acquisition.
    fetchrow_side_effects:
        When set, replaces ``conn.fetchrow`` with an ``AsyncMock`` whose
        ``side_effect`` iterates through the provided list of return values.
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
    """Build a minimal request mock with ``request.state.user_id`` (+ optional role).

    Parameters
    ----------
    user_id:
        Value placed on ``request.state.user_id``.
    role:
        When provided, also sets ``request.state.user_role``.
    **state_overrides:
        Any additional attributes to place on ``request.state``.
    """
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
# Contract-layer fixture factories (Wave 4)
#
# Idiomatic-mock carve-out: the following external boundaries MUST keep
# AsyncMock / @patch and must NOT be collapsed into contract_conn:
#   - Ollama embed (httpx call in embed_texts)
#   - Qdrant query (qdrant_client)
#   - Telegram Bot API (python-telegram-bot Application)
#   - OpenAI / Instructor (openai.AsyncOpenAI)
#   - Langfuse trace/span (langfuse SDK)
#   - task_registry._TASK_MAP (module-level mutable dict)
# These boundaries own their own I/O contract; only asyncpg pool mocks
# collapse into contract_conn. This carve-out registry is the load-bearing
# rule for Sub-wave 4.4 test migrations.
# ---------------------------------------------------------------------------


def make_contract_pg_dsn(container_prefix: str):  # -> pytest fixture (session scope)
    """Return a session-scoped contract_pg_dsn pytest fixture for *container_prefix*.

    Spawns ONE postgres:16.8 container per pytest session, applies the
    canonical schema (db/init.sql) + run_migrations() once, yields the DSN
    for the lifetime of the session. The per-test rollback isolation is
    provided by the function-scoped contract_conn fixture below.
    """

    @pytest.fixture(scope="session")
    def contract_pg_dsn() -> str:  # type: ignore[return]
        if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
            pytest.skip(
                "set JARVIS_RUN_LIVE_PG=1 to run contract-layer tests "
                "(DB-backed; session-scoped postgres container)"
            )
        if shutil.which("docker") is None:
            pytest.fail("Docker CLI is required for contract-layer tests")

        container = f"{container_prefix}-contract-{uuid.uuid4().hex[:12]}"
        password = f"jarvis-contract-{uuid.uuid4().hex}"
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
            dsn = f"postgresql://jarvis:{password}@127.0.0.1:{host_port}/jarvis"
            yield dsn
        finally:
            _docker_cli(["rm", "-f", container], check=False, timeout=10)

    return contract_pg_dsn


def _make_contract_pool_fixture():
    """Return a session-scoped fixture providing an asyncpg pool against
    contract_pg_dsn. Applies db/init.sql + run_migrations() once per session."""
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
    conn.transaction() nest correctly inside this outer transaction."""
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


class SharedAcquireCM:
    """Async CM returned by SharedConnPool.acquire(); always yields the same conn."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class SharedConnPool:
    """Pool-shaped object that always returns a single shared connection from acquire().

    Lets a service's FastAPI app see the SAME asyncpg connection (and therefore
    the same outer transaction) as the test's contract_conn fixture, so writes
    from both ends share one rollback boundary.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> SharedAcquireCM:  # not async — returns an async-CM
        return SharedAcquireCM(self._conn)

    async def close(self) -> None:  # idempotent; the real pool's lifecycle is the fixture's
        return None


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
    contract_conn transaction so the seed is contained by per-test rollback."""
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="function", loop_scope="session")
    async def contract_two_users(contract_conn) -> TwoUsers:
        user_a_id, cookie_a = await _seed_user(contract_conn, "iso-user-a@contract.test")
        user_b_id, cookie_b = await _seed_user(contract_conn, "iso-user-b@contract.test")
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
