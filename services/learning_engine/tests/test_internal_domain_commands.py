"""Live contracts for Learning's idempotent owner-command inbox."""

from __future__ import annotations

import uuid
from datetime import date

import asyncpg
import pytest
import pytest_asyncio
from jarvis_common.db_helpers import init_pg_connection

from learning_engine.repos.domain_commands import apply_command

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def learning_runtime_pool(contract_pg_dsn, _contract_pool):
    """Yield a real Learning runtime pool against the initialized contract DB."""
    password = "learning-domain-command-contract-password"
    bootstrap = await asyncpg.connect(contract_pg_dsn)
    await bootstrap.execute(
        "ALTER ROLE jarvis_learning_runtime LOGIN "
        "PASSWORD 'learning-domain-command-contract-password'"
    )
    pool = await asyncpg.create_pool(
        contract_pg_dsn,
        user="jarvis_learning_runtime",
        password=password,
        min_size=1,
        max_size=2,
        init=init_pg_connection,
    )
    try:
        yield bootstrap, pool
    finally:
        await pool.close()
        await bootstrap.close()


async def test_paper_read_inbox_applies_once_for_duplicate_request_id(
    learning_runtime_pool,
) -> None:
    """One durable event increments activity exactly once."""
    bootstrap, pool = learning_runtime_pool
    user_id = await bootstrap.fetchval(
        "INSERT INTO platform.users (email, role) VALUES ($1, 'user') RETURNING id",
        f"paper-read-{uuid.uuid4().hex}@example.com",
    )
    request_id = str(uuid.uuid4())

    first = await apply_command(
        pool,
        command_type="paper.read",
        request_id=request_id,
        user_id=user_id,
        payload={"paper_id": 42},
    )
    duplicate = await apply_command(
        pool,
        command_type="paper.read",
        request_id=request_id,
        user_id=user_id,
        payload={"paper_id": 42},
    )

    assert first is True
    assert duplicate is False
    assert (
        await bootstrap.fetchval(
            "SELECT papers_read FROM learning.daily_log WHERE user_id = $1",
            user_id,
        )
        == 1
    )

    with pytest.raises(ValueError, match="different command"):
        await apply_command(
            pool,
            command_type="paper.read",
            request_id=request_id,
            user_id=user_id,
            payload={"paper_id": 99},
        )

    with pytest.raises(ValueError, match="different command"):
        await apply_command(
            pool,
            command_type="paper.read",
            request_id=request_id,
            user_id=user_id,
            payload={"paper_id": 42, "unexpected": True},
        )


async def test_journal_upsert_writes_one_row_per_user_day(
    learning_runtime_pool,
) -> None:
    """A saved journal day lands once and a later save replaces it in place."""
    bootstrap, pool = learning_runtime_pool
    user_id = await bootstrap.fetchval(
        "INSERT INTO platform.users (email, role) VALUES ($1, 'user') RETURNING id",
        f"journal-upsert-{uuid.uuid4().hex}@example.com",
    )
    entry_date = date(2031, 5, 17)
    stored_entry = """SELECT date, prompts->>'worked' AS worked
                      FROM learning.journal_entries WHERE user_id = $1"""

    created = await apply_command(
        pool,
        command_type="journal.upsert",
        request_id=str(uuid.uuid4()),
        user_id=user_id,
        payload={"date": entry_date.isoformat(), "prompts": {"worked": "first draft"}},
    )
    entry = await bootstrap.fetchrow(stored_entry, user_id)

    assert created is True
    assert entry is not None, "journal.upsert did not persist the day"
    assert entry["date"] == entry_date
    assert entry["worked"] == "first draft"

    rewritten = await apply_command(
        pool,
        command_type="journal.upsert",
        request_id=str(uuid.uuid4()),
        user_id=user_id,
        payload={"date": entry_date.isoformat(), "prompts": {"worked": "revised"}},
    )
    saved_days = await bootstrap.fetchval(
        "SELECT COUNT(*) FROM learning.journal_entries WHERE user_id = $1",
        user_id,
    )
    updated = await bootstrap.fetchrow(stored_entry, user_id)

    assert rewritten is True
    assert saved_days == 1, "a second save must replace the day, not add a row"
    assert updated["worked"] == "revised"


async def test_user_erasure_is_idempotent_and_leaves_no_learning_subject_rows(
    learning_runtime_pool,
) -> None:
    """The fixed owner capability removes every Learning row keyed to the user."""
    bootstrap, pool = learning_runtime_pool
    user_id = await bootstrap.fetchval(
        "INSERT INTO platform.users (email, role) VALUES ($1, 'user') RETURNING id",
        f"learning-erasure-{uuid.uuid4().hex}@example.com",
    )
    request_id = str(uuid.uuid4())
    await bootstrap.execute(
        """INSERT INTO learning.daily_log (user_id, log_date, papers_read)
           VALUES ($1, CURRENT_DATE, 3)""",
        user_id,
    )
    await bootstrap.execute(
        """INSERT INTO learning.daily_intent (user_id, intent_date, intent_text)
           VALUES ($1, CURRENT_DATE, 'erase me')""",
        user_id,
    )

    first = await apply_command(
        pool,
        command_type="user.erase",
        request_id=request_id,
        user_id=user_id,
        payload={},
    )
    duplicate = await apply_command(
        pool,
        command_type="user.erase",
        request_id=request_id,
        user_id=user_id,
        payload={},
    )

    assert first is True
    assert duplicate is False
    table_names = await bootstrap.fetch(
        """SELECT table_name FROM information_schema.columns
           WHERE table_schema = 'learning' AND column_name = 'user_id'"""
    )
    assert table_names
    for row in table_names:
        table_name = str(row["table_name"])
        assert table_name.replace("_", "").isalnum()
        residual = await bootstrap.fetchval(
            f'SELECT COUNT(*) FROM learning."{table_name}" WHERE user_id = $1',
            user_id,
        )
        assert residual == 0, f"Learning erasure left rows in {table_name}"
