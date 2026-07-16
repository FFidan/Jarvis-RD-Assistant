"""Regression: PATCH /api/account must not deadlock a saturated pool.

``update_account`` opens ONE ``pool.acquire()`` and, on base, called
``log_audit`` / ``send_magic_link`` *while still holding it*. Those helpers each
RE-ACQUIRE a separate connection from the SAME pool, which has no per-acquire
timeout — so on a saturated pool the nested acquire waits forever (hold-and-wait
deadlock). With ``max_size=1`` even a single request deadlocks: the outer holds
the only slot while the nested acquire blocks on it.

This test wires a REAL ``max_size=1`` asyncpg pool and fires concurrent handler
calls. On base the first call hangs — ``asyncio.wait_for`` is therefore
MANDATORY: it converts the hang into a catchable ``TimeoutError`` instead of
letting pytest hang forever. After the fix (side-effects run only after the
connection is released) every call completes.

Live-PG only: gated by ``JARVIS_RUN_LIVE_PG=1`` via the ``live_pg`` marker
(excluded by the default ``addopts``); uses the disposable ``postgres:16``
container behind the ``live_pg_dsn`` fixture (see ``conftest.py`` /
``testing_db.make_live_pg_dsn``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

pytestmark = pytest.mark.live_pg


@pytest.fixture(autouse=True)
def _disable_limiter():
    """Await the ``@limiter.limit``-decorated handler directly (mirror test_account_router)."""
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


class _FakeURL:
    path = "/api/account"

    def replace(self, **_kwargs) -> str:
        return "http://test/account/confirm-email?token=tok"

    def __str__(self) -> str:
        return "http://test/api/account"


def _build_request(pool) -> SimpleNamespace:
    """SimpleNamespace request with the REAL pool on ``app.state.db_pool``."""
    state = SimpleNamespace(user_id=1)
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    return SimpleNamespace(state=state, app=app, url=_FakeURL())


async def _make_max1_pool(live_pg_dsn: str) -> asyncpg.Pool:
    """Real asyncpg pool with **max_size=1** + full schema applied.

    Copy of ``conftest.test_db_pool`` (init.sql baseline + run_migrations) but
    capped at a single connection — the smallest pool that reproduces the
    hold-and-wait deadlock deterministically.
    """
    from jarvis_common.db_helpers import init_pg_connection
    from jarvis_common.migrations import run_migrations

    db_dir = Path(__file__).parent.parent.parent.parent / "db"
    init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
    migrations_dir = db_dir / "migrations"

    pool = None
    for attempt in range(10):
        try:
            pool = await asyncpg.create_pool(
                live_pg_dsn, min_size=1, max_size=1, init=init_pg_connection
            )
            break
        except (OSError, asyncpg.PostgresError):
            if attempt == 9:
                raise
            await asyncio.sleep(0.5)
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    await run_migrations(pool, migrations_dir=migrations_dir)
    return pool


async def test_update_account_display_name_does_not_deadlock_max1_pool(live_pg_dsn: str) -> None:
    """10 concurrent display_name updates must all complete on a max_size=1 pool.

    display_name path triggers the ``log_audit`` nested acquire with no SMTP
    dependency. On base the first call deadlocks the single slot -> TimeoutError.
    """
    from paper_ingestion.models.account import AccountUpdate, AccountUpdateResponse
    from paper_ingestion.routers.account import update_account

    pool = await _make_max1_pool(live_pg_dsn)
    try:
        # Seed a user in a PRIOR acquire block that FULLY CLOSES so the single
        # slot is free before the concurrent handlers run.
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, role) "
                "VALUES ('deadlock@test.example', 'user') RETURNING id"
            )

        coros = [
            update_account(
                body=AccountUpdate(display_name=f"name-{i}"),
                request=_build_request(pool),
                user_id=uid,
            )
            for i in range(10)
        ]
        # wait_for is MANDATORY: on base the nested acquire hangs forever; this
        # turns the deadlock into a catchable TimeoutError instead of a hung run.
        results = await asyncio.wait_for(asyncio.gather(*coros), timeout=15)

        assert len(results) == 10
        assert all(isinstance(r, AccountUpdateResponse) for r in results)
        # Concurrent writers race to the same row, so any re-read reflects one of
        # the 10 names we wrote (no other writer exists) — never a stale/None value.
        expected_names = {f"name-{j}" for j in range(10)}
        assert all(r.account.display_name in expected_names for r in results)
    finally:
        await pool.close()


async def test_update_account_email_change_does_not_deadlock_max1_pool(live_pg_dsn: str) -> None:
    """Email-change path (send_magic_link + _effective_smtp + log_audit nested acquires).

    Exercises the second family of nested acquires. SMTP is unconfigured in the
    test DB so send_magic_link takes its dev/undeliverable fallback (which itself
    re-acquires via log_event) — all after the outer connection is released.
    """
    from paper_ingestion.models.account import AccountUpdate, AccountUpdateResponse
    from paper_ingestion.routers.account import update_account

    pool = await _make_max1_pool(live_pg_dsn)
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, role) "
                "VALUES ('deadlock2@test.example', 'user') RETURNING id"
            )

        result = await asyncio.wait_for(
            update_account(
                body=AccountUpdate(email="changed@test.example"),
                request=_build_request(pool),
                user_id=uid,
            ),
            timeout=15,
        )
        assert isinstance(result, AccountUpdateResponse)
        # Invariant (e): email is NOT swapped until the token is confirmed.
        assert result.account.email == "deadlock2@test.example"
    finally:
        await pool.close()


async def test_update_account_email_clash_still_audits_display_name(live_pg_dsn: str) -> None:
    """Same-request display_name change is applied AND audited even when the email clashes.

    Regression: deferring ``log_audit`` to after the acquire block must not drop
    the staged ``account.display_name.update`` event when the email-clash 409
    fires. The 409 is deferred past the audit flush so base behaviour holds —
    display_name is persisted and audited, and the caller still gets the 409.
    """
    from fastapi import HTTPException

    from paper_ingestion.models.account import AccountUpdate
    from paper_ingestion.routers.account import update_account

    pool = await _make_max1_pool(live_pg_dsn)
    try:
        # Seed caller A and a DISTINCT user B who already owns the target email.
        async with pool.acquire() as conn:
            uid_a = await conn.fetchval(
                "INSERT INTO users (email, role) "
                "VALUES ('clash-a@test.example', 'user') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO users (email, role) VALUES ('taken@test.example', 'user')"
            )

        with pytest.raises(HTTPException) as exc_info:
            await update_account(
                body=AccountUpdate(display_name="renamed", email="taken@test.example"),
                request=_build_request(pool),
                user_id=uid_a,
            )
        assert exc_info.value.status_code == 409

        async with pool.acquire() as conn:
            # (2) display_name was persisted immediately despite the email 409.
            name = await conn.fetchval("SELECT display_name FROM users WHERE id = $1", uid_a)
            assert name == "renamed"
            # (3) the display_name update was audited — the staged event still flushed.
            audit_count = await conn.fetchval(
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'account.display_name.update' AND user_id = $1",
                str(uid_a),
            )
            assert audit_count == 1
    finally:
        await pool.close()
