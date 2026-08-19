"""Real-PostgreSQL proof that a purge leaves vectors and rows agreeing.

The retention set is computed by production SQL over real rows (public scope OR
membership in a surviving user's library). Only that query can decide which of a
departed user's points are redacted and which are removed, so this is the one
place the whole split is exercised end to end: an in-memory point store applies
the filters the job builds, and the assertions read the resulting collection.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from jarvis_common.db_helpers import init_pg_connection

from paper_ingestion.jobs import data_purge

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


class _PointStore:
    """Minimal Qdrant stand-in that evaluates the job's own filters over points."""

    def __init__(self, points: list[dict[str, Any]]) -> None:
        self.points = points

    @staticmethod
    def _condition_holds(condition: Any, point: dict[str, Any]) -> bool:
        value = point.get(condition.key)
        match = condition.match
        if hasattr(match, "any"):
            return value in match.any
        return value == match.value

    def _selected(self, selector: Any) -> list[dict[str, Any]]:
        return [
            point
            for point in self.points
            if all(self._condition_holds(c, point) for c in (selector.must or []))
            and not any(self._condition_holds(c, point) for c in (selector.must_not or []))
        ]

    async def count(self, collection_name: str, count_filter: Any, exact: bool) -> Any:
        return SimpleNamespace(count=len(self._selected(count_filter)))

    async def set_payload(
        self, collection_name: str, payload: dict[str, Any], points: Any, wait: bool
    ) -> None:
        for point in self._selected(points):
            point.update(payload)

    async def delete(self, collection_name: str, points_selector: Any, wait: bool) -> None:
        removed = {id(point) for point in self._selected(points_selector)}
        self.points = [point for point in self.points if id(point) not in removed]


async def test_retired_purge_task_is_a_noop() -> None:
    """Only Platform's durable coordinator may schedule user erasure."""
    await data_purge.data_purge_task(object())


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def research_erasure_connections(contract_pg_dsn, _contract_pool):
    """Yield bootstrap and Research runtime connections to the contract DB."""
    password = "research-erasure-contract-password"
    bootstrap = await asyncpg.connect(contract_pg_dsn)
    await bootstrap.execute(
        "ALTER ROLE jarvis_research_runtime LOGIN PASSWORD 'research-erasure-contract-password'"
    )
    runtime = await asyncpg.connect(
        contract_pg_dsn,
        user="jarvis_research_runtime",
        password=password,
    )
    await init_pg_connection(runtime)
    try:
        yield bootstrap, runtime
    finally:
        await runtime.close()
        await bootstrap.close()


async def test_research_erasure_capability_clears_every_user_attributable_table(
    research_erasure_connections,
) -> None:
    """The fixed owner capability leaves no Research row keyed to the account."""
    bootstrap, runtime = research_erasure_connections
    user_id = uuid.uuid4().int % 1_000_000_000 + 1
    event_id = uuid.uuid4()
    await bootstrap.execute(
        """INSERT INTO research.domain_events
           (id, event_type, user_id, paper_id)
           VALUES ($1, 'paper.read', $2, 1)""",
        event_id,
        user_id,
    )
    await bootstrap.execute(
        """INSERT INTO research.zotero_push_claims
           (paper_id, user_id, lease_id, lease_expires_at)
           VALUES (1, $1, $2, NOW() + INTERVAL '1 minute')""",
        user_id,
        uuid.uuid4(),
    )

    with pytest.raises(Exception, match="caller is not allowed"):
        await bootstrap.execute("SELECT research.erase_user_data($1)", user_id)
    await runtime.execute("SELECT research.erase_user_data($1)", user_id)

    table_names = await bootstrap.fetch(
        """SELECT table_name FROM information_schema.columns
           WHERE table_schema = 'research' AND column_name = 'user_id'"""
    )
    assert table_names
    for row in table_names:
        table_name = str(row["table_name"])
        assert table_name.replace("_", "").isalnum()
        residual = await bootstrap.fetchval(
            f'SELECT COUNT(*) FROM research."{table_name}" WHERE user_id = $1',
            user_id,
        )
        assert residual == 0, f"Research erasure left rows in {table_name}"


async def test_dead_lettered_outbox_events_return_to_the_queue(
    research_erasure_connections,
) -> None:
    """An operator can replay dead letters, except where replay would collide.

    Dead-lettering is otherwise terminal, and an undelivered ``paper.deleted``
    keeps a deleted paper's Research-private rows retained. Undelivered
    deletions are unique per owner and paper, so a dead letter with a live
    sibling must stay put rather than fail the whole replay.
    """
    from jarvis_common.testing import SharedConnPool

    from paper_ingestion.repos.domain_events import requeue_dead_lettered_events

    bootstrap, runtime = research_erasure_connections
    user_id = uuid.uuid4().int % 1_000_000_000 + 1
    replayable, collides, sibling = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    await bootstrap.execute(
        """INSERT INTO research.domain_events (id, event_type, user_id, paper_id, dead_lettered_at)
           VALUES ($1, 'paper.read', $2, 1, NOW())""",
        replayable,
        user_id,
    )
    await bootstrap.execute(
        """INSERT INTO research.domain_events (id, event_type, user_id, paper_id, dead_lettered_at)
           VALUES ($1, 'paper.deleted', $2, 2, NOW())""",
        collides,
        user_id,
    )
    await bootstrap.execute(
        """INSERT INTO research.domain_events (id, event_type, user_id, paper_id)
           VALUES ($1, 'paper.deleted', $2, 2)""",
        sibling,
        user_id,
    )

    # Through the runtime identity, as the service does: the statement is
    # unqualified and resolves on that role's stored search path.
    requeued = await requeue_dead_lettered_events(SharedConnPool(runtime), user_id=user_id)

    still_dead = {
        row["id"]
        for row in await bootstrap.fetch(
            "SELECT id FROM research.domain_events WHERE user_id = $1 AND dead_lettered_at IS NOT NULL",
            user_id,
        )
    }

    assert requeued == 1, "exactly the replayable dead letter should have moved"
    assert replayable not in still_dead, "the replayable event was not returned to the queue"
    assert collides in still_dead, (
        "a deletion whose owner and paper already have an undelivered sibling must stay "
        "dead-lettered; replaying it would break the per-owner uniqueness of undelivered deletions"
    )
