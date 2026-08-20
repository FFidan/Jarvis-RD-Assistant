"""Real-PostgreSQL proof that a purge leaves vectors and rows agreeing.

The retention set is computed by production SQL over real rows (public scope OR
membership in a surviving user's library). Only that query can decide which of a
departed user's points are redacted and which are removed, so this is the one
place the whole split is exercised end to end: an in-memory point store applies
the filters the job builds, and the assertions read the resulting collection.
"""

from __future__ import annotations

import contextlib
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


async def _seed_paper(
    bootstrap: asyncpg.Connection, external_id: str, visibility_scope: str
) -> int:
    """Insert one paper with its chunk and return its identifier."""
    paper_id = await bootstrap.fetchval(
        """INSERT INTO research.papers
           (external_id, source_type, title, authors, url, visibility_scope,
            pdf_local_path, pdf_downloaded)
           VALUES ($1, 'arxiv', 'Erasure fixture', ARRAY['A. Author'],
                   $2, $3, $4, TRUE)
           RETURNING id""",
        external_id,
        f"https://example.invalid/{external_id}",
        visibility_scope,
        f"{external_id}.pdf",
    )
    await bootstrap.execute(
        """INSERT INTO research.paper_chunks (paper_id, chunk_index, content)
           VALUES ($1, 0, 'uploaded document text')""",
        paper_id,
    )
    return int(paper_id)


async def test_erasure_removes_sole_owned_private_papers_and_keeps_the_rest(
    research_erasure_connections, tmp_path, monkeypatch
) -> None:
    """A departed account's own papers go; public and co-held papers stay."""
    from paper_ingestion.services import paper_content_reclaim

    bootstrap, runtime = research_erasure_connections
    user_id = uuid.uuid4().int % 1_000_000_000 + 1
    survivor_id = user_id + 1
    tag = uuid.uuid4().hex[:8]

    for account in (user_id, survivor_id):
        await bootstrap.execute(
            "INSERT INTO platform.users (id, email) VALUES ($1, $2)",
            account,
            f"erasure-{account}@example.invalid",
        )
    sole_owned = await _seed_paper(bootstrap, f"sole-{tag}", "private")
    co_held = await _seed_paper(bootstrap, f"shared-{tag}", "private")
    public = await _seed_paper(bootstrap, f"public-{tag}", "public")
    for paper_id in (sole_owned, co_held, public):
        await bootstrap.execute(
            """INSERT INTO research.user_library (user_id, paper_id, added_via)
               VALUES ($1, $2, 'manual_save')""",
            user_id,
            paper_id,
        )
    await bootstrap.execute(
        """INSERT INTO research.user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        survivor_id,
        co_held,
    )

    storage = tmp_path / "pdfs"
    snapshots = tmp_path / "snapshots"
    storage.mkdir()
    snapshots.mkdir()
    for paper_id in (sole_owned, co_held, public):
        (storage / f"{paper_id}.pdf").write_bytes(b"%PDF-1.4 fixture")
        (snapshots / str(paper_id)).mkdir()
        (snapshots / str(paper_id) / "1.png").write_bytes(b"page")
    monkeypatch.setattr(paper_content_reclaim, "PDF_STORAGE_PATH", str(storage))
    monkeypatch.setattr(paper_content_reclaim, "SNAPSHOT_STORAGE_PATH", str(snapshots))

    removed = await paper_content_reclaim.erase_orphaned_user_papers(runtime, user_id)

    assert removed == [sole_owned]
    assert (
        await bootstrap.fetchval("SELECT COUNT(*) FROM research.papers WHERE id = $1", sole_owned)
        == 0
    )
    assert (
        await bootstrap.fetchval(
            "SELECT COUNT(*) FROM research.paper_chunks WHERE paper_id = $1", sole_owned
        )
        == 0
    )
    assert not (storage / f"{sole_owned}.pdf").exists()
    assert not (snapshots / str(sole_owned)).exists()
    for kept in (co_held, public):
        assert (
            await bootstrap.fetchval("SELECT COUNT(*) FROM research.papers WHERE id = $1", kept)
            == 1
        )
        assert (
            await bootstrap.fetchval(
                "SELECT COUNT(*) FROM research.paper_chunks WHERE paper_id = $1", kept
            )
            == 1
        )
        assert (storage / f"{kept}.pdf").exists()
        assert (snapshots / str(kept)).exists()

    await bootstrap.execute(
        "DELETE FROM research.papers WHERE id = ANY($1::int[])", [co_held, public]
    )
    await bootstrap.execute(
        "DELETE FROM platform.users WHERE id = ANY($1::bigint[])", [user_id, survivor_id]
    )


async def test_a_document_that_cannot_be_reclaimed_leaves_the_work_outstanding(
    research_erasure_connections, tmp_path, monkeypatch
) -> None:
    """A failed reclaim unwinds the deletion instead of reporting a finished erasure.

    If the rows were already gone the retry would find no candidates, the phase
    would advance, and the request would reach its completed state with the
    documents still on disk and nobody told.
    """
    from paper_ingestion.services import paper_content_reclaim

    bootstrap, runtime = research_erasure_connections
    user_id = uuid.uuid4().int % 1_000_000_000 + 1
    tag = uuid.uuid4().hex[:8]

    await bootstrap.execute(
        "INSERT INTO platform.users (id, email) VALUES ($1, $2)",
        user_id,
        f"unreclaimable-{user_id}@example.invalid",
    )
    stranded = await _seed_paper(bootstrap, f"stranded-{tag}", "private")
    await bootstrap.execute(
        """INSERT INTO research.user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        user_id,
        stranded,
    )

    storage = tmp_path / "pdfs"
    snapshots = tmp_path / "snapshots"
    storage.mkdir()
    snapshots.mkdir()
    monkeypatch.setattr(paper_content_reclaim, "PDF_STORAGE_PATH", str(storage))
    monkeypatch.setattr(paper_content_reclaim, "SNAPSHOT_STORAGE_PATH", str(snapshots))

    async def _refuse(paper_ids) -> None:
        raise OSError("stored content could not be reclaimed")

    monkeypatch.setattr(paper_content_reclaim, "_remove_stored_paper_files", _refuse)

    with pytest.raises(OSError):
        await paper_content_reclaim.erase_orphaned_user_papers(runtime, user_id)

    assert (
        await bootstrap.fetchval("SELECT COUNT(*) FROM research.papers WHERE id = $1", stranded)
        == 1
    ), "the record was deleted although its document could not be reclaimed"
    assert (
        await bootstrap.fetchval(
            "SELECT COUNT(*) FROM research.user_library WHERE paper_id = $1", stranded
        )
        == 1
    ), "the paper is no longer a candidate, so a retry would report a finished erasure"

    await bootstrap.execute("DELETE FROM research.papers WHERE id = $1", stranded)
    await bootstrap.execute("DELETE FROM platform.users WHERE id = $1", user_id)


async def test_a_paper_another_account_is_still_discarding_is_kept(
    research_erasure_connections, tmp_path, monkeypatch
) -> None:
    """A cleanup another account started has to finish before the paper can go.

    Removing the paper cascades the pending row away, and nothing in the
    Learning schema references a paper by foreign key, so that row is the only
    signal Learning ever gets that its projection should go too.
    """
    from paper_ingestion.services import paper_content_reclaim

    bootstrap, runtime = research_erasure_connections
    user_id = uuid.uuid4().int % 1_000_000_000 + 1
    departing_id = user_id + 1
    tag = uuid.uuid4().hex[:8]

    for account in (user_id, departing_id):
        await bootstrap.execute(
            "INSERT INTO platform.users (id, email) VALUES ($1, $2)",
            account,
            f"discarding-{account}@example.invalid",
        )
    discarding = await _seed_paper(bootstrap, f"discarding-{tag}", "private")
    await bootstrap.execute(
        """INSERT INTO research.user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        user_id,
        discarding,
    )
    event_id = uuid.uuid4()
    await bootstrap.execute(
        """INSERT INTO research.domain_events (id, event_type, user_id, paper_id)
           VALUES ($1, 'paper.deleted', $2, $3)""",
        event_id,
        departing_id,
        discarding,
    )
    await bootstrap.execute(
        """INSERT INTO research.pending_paper_deletions (event_id, user_id, paper_id)
           VALUES ($1, $2, $3)""",
        event_id,
        departing_id,
        discarding,
    )

    storage = tmp_path / "pdfs"
    snapshots = tmp_path / "snapshots"
    storage.mkdir()
    snapshots.mkdir()
    (storage / f"{discarding}.pdf").write_bytes(b"%PDF-1.4 fixture")
    monkeypatch.setattr(paper_content_reclaim, "PDF_STORAGE_PATH", str(storage))
    monkeypatch.setattr(paper_content_reclaim, "SNAPSHOT_STORAGE_PATH", str(snapshots))

    removed = await paper_content_reclaim.erase_orphaned_user_papers(runtime, user_id)

    assert removed == []
    assert (
        await bootstrap.fetchval(
            "SELECT COUNT(*) FROM research.pending_paper_deletions WHERE event_id = $1", event_id
        )
        == 1
    ), "a cleanup another account had not finished was cut short"
    assert (
        await bootstrap.fetchval("SELECT COUNT(*) FROM research.papers WHERE id = $1", discarding)
        == 1
    )
    assert (storage / f"{discarding}.pdf").exists()

    await bootstrap.execute("DELETE FROM research.papers WHERE id = $1", discarding)
    await bootstrap.execute(
        "DELETE FROM platform.users WHERE id = ANY($1::bigint[])", [user_id, departing_id]
    )


async def test_a_paper_claimed_published_or_discarded_mid_erasure_is_kept(
    research_erasure_connections, tmp_path, monkeypatch
) -> None:
    """The removal re-checks the rule instead of trusting the set it selected.

    Candidates are read before the publication lock is taken. A paper another
    researcher claims, the deployment publishes, or another account starts
    discarding inside that window is no longer the erased account's alone, and
    the delete has to see that.
    """
    from paper_ingestion.services import paper_content_reclaim

    bootstrap, runtime = research_erasure_connections
    user_id = uuid.uuid4().int % 1_000_000_000 + 1
    survivor_id = user_id + 1
    tag = uuid.uuid4().hex[:8]

    for account in (user_id, survivor_id):
        await bootstrap.execute(
            "INSERT INTO platform.users (id, email) VALUES ($1, $2)",
            account,
            f"midflight-{account}@example.invalid",
        )
    claimed = await _seed_paper(bootstrap, f"claimed-{tag}", "private")
    published = await _seed_paper(bootstrap, f"published-{tag}", "private")
    discarded = await _seed_paper(bootstrap, f"discarded-{tag}", "private")
    discard_event = uuid.uuid4()
    for paper_id in (claimed, published, discarded):
        await bootstrap.execute(
            """INSERT INTO research.user_library (user_id, paper_id, added_via)
               VALUES ($1, $2, 'manual_save')""",
            user_id,
            paper_id,
        )

    storage = tmp_path / "pdfs"
    snapshots = tmp_path / "snapshots"
    storage.mkdir()
    snapshots.mkdir()
    for paper_id in (claimed, published, discarded):
        (storage / f"{paper_id}.pdf").write_bytes(b"%PDF-1.4 fixture")
        (snapshots / str(paper_id)).mkdir()
        (snapshots / str(paper_id) / "1.png").write_bytes(b"page")
    monkeypatch.setattr(paper_content_reclaim, "PDF_STORAGE_PATH", str(storage))
    monkeypatch.setattr(paper_content_reclaim, "SNAPSHOT_STORAGE_PATH", str(snapshots))

    take_lock = paper_content_reclaim.pdf_publish_operation

    @contextlib.asynccontextmanager
    async def _claim_and_publish_inside_the_lock(path):
        """Move both papers out of reach after the candidates were selected."""
        async with take_lock(path):
            await bootstrap.execute(
                """INSERT INTO research.user_library (user_id, paper_id, added_via)
                   VALUES ($1, $2, 'manual_save')""",
                survivor_id,
                claimed,
            )
            await bootstrap.execute(
                "UPDATE research.papers SET visibility_scope = 'public' WHERE id = $1",
                published,
            )
            await bootstrap.execute(
                """INSERT INTO research.domain_events (id, event_type, user_id, paper_id)
                   VALUES ($1, 'paper.deleted', $2, $3)""",
                discard_event,
                survivor_id,
                discarded,
            )
            await bootstrap.execute(
                """INSERT INTO research.pending_paper_deletions (event_id, user_id, paper_id)
                   VALUES ($1, $2, $3)""",
                discard_event,
                survivor_id,
                discarded,
            )
            yield

    monkeypatch.setattr(
        paper_content_reclaim, "pdf_publish_operation", _claim_and_publish_inside_the_lock
    )

    removed = await paper_content_reclaim.erase_orphaned_user_papers(runtime, user_id)

    assert (
        await bootstrap.fetchval("SELECT COUNT(*) FROM research.papers WHERE id = $1", claimed) == 1
    ), "a paper another researcher claimed mid-erasure was deleted"
    assert (
        await bootstrap.fetchval("SELECT COUNT(*) FROM research.papers WHERE id = $1", published)
        == 1
    ), "a paper the deployment published mid-erasure was deleted"
    assert removed == []
    assert (
        await bootstrap.fetchval(
            "SELECT COUNT(*) FROM research.pending_paper_deletions WHERE event_id = $1",
            discard_event,
        )
        == 1
    ), "a cleanup another account started mid-erasure was cut short"
    for kept in (claimed, published, discarded):
        assert (storage / f"{kept}.pdf").exists(), (
            "the stored document of a paper the re-check kept was destroyed"
        )
        assert (snapshots / str(kept)).exists(), (
            "the page images of a paper the re-check kept were destroyed"
        )

    await bootstrap.execute(
        "DELETE FROM research.papers WHERE id = ANY($1::int[])", [claimed, published, discarded]
    )
    await bootstrap.execute(
        "DELETE FROM platform.users WHERE id = ANY($1::bigint[])", [user_id, survivor_id]
    )
