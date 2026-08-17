"""auto_fetch fans out a canonical paper into N user_library rows.

Approach: patch ``upsert_verified_public_paper`` and ``fan_out_to_topic_users`` and assert
the wiring (one canonical insert per result, one fan-out call per topic
per result).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from jarvis_common.library import fan_out_to_topic_users, list_users_with_topic
from paper_ingestion.models import PaperCreate, TopicRef
from paper_ingestion.pipelines import auto_fetch as af
from tests._paper_fakes import make_paper_create


def _make_paper(idx: int = 0) -> PaperCreate:
    """Build a distinct paper returned by a source poll."""
    return make_paper_create(
        external_id=f"arxiv:1000.0000{idx}",
        title=f"Paper {idx}",
        authors=["Alice"],
        abstract="abs",
        url="https://example.org",
    )


@pytest.mark.asyncio
async def test_run_auto_pipeline_fans_out_per_topic(monkeypatch):
    """Each new canonical paper triggers a fan-out call passing the matching
    topic_id. With one topic and three papers, fan-out is invoked three times."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    # Mock DB pool with two contexts: source/topic query and per-paper inserts.
    conn = AsyncMock()
    sources_records = [
        {
            "id": 1,
            "source_type": "arxiv",
            "enabled": True,
            "config": {},
            "display_order": 1,
        }
    ]

    # First fetch returns the sources, second returns the topics.
    conn.fetch = AsyncMock(
        side_effect=[
            sources_records,  # sources query
            [{"id": 11, "name": "diffusion"}],  # topics query
            [],  # to_download
            [],  # to_process
        ]
    )

    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)

    # 3 fake papers from the source.
    fake_source = MagicMock()
    fake_source.fetch_new_since = AsyncMock(return_value=[_make_paper(i) for i in range(3)])

    upsert_calls: list[int] = []

    async def fake_upsert(conn_arg, paper, **kw):
        upsert_calls.append(len(upsert_calls) + 1)
        return {"id": 100 + len(upsert_calls), "is_insert": True}

    fanout_calls: list[tuple[int, list[int]]] = []

    async def fake_fanout(conn_arg, *, paper_id, topic_ids):
        fanout_calls.append((paper_id, list(topic_ids)))
        return len(topic_ids)

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )
    )

    with (
        patch(
            "paper_ingestion.pipelines.auto_fetch.get_source_class",
            return_value=lambda *a, **kw: fake_source,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.upsert_verified_public_paper",
            side_effect=fake_upsert,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.fan_out_to_topic_users", side_effect=fake_fanout
        ),
    ):
        await af.run_auto_pipeline(app)

    # 3 papers × 1 topic = 3 upsert calls and 3 fan-out calls.
    assert len(upsert_calls) == 3, f"expected 3 upserts, got {upsert_calls}"
    assert len(fanout_calls) == 3, f"expected 3 fan-outs, got {fanout_calls}"
    # Every fan-out call carries the single configured topic_id.
    assert all(call[1] == [11] for call in fanout_calls)
    # Discovery now goes through fetch_new_since (incremental), not search.
    fake_source.fetch_new_since.assert_awaited_once()
    topics_arg = fake_source.fetch_new_since.await_args.args[1]
    assert topics_arg == [TopicRef(id=11, name="diffusion")]


@pytest.mark.asyncio
async def test_run_auto_pipeline_threads_configured_query_terms(monkeypatch):
    """The source receives each topic's configured ``query_terms``, not just its name."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [{"id": 1, "source_type": "arxiv", "enabled": True, "config": {}, "display_order": 1}],
            [{"id": 9, "name": "RL", "query_terms": ["reinforcement learning"]}],  # topics query
            [],  # to_download
            [],  # to_process
        ]
    )

    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)

    fake_source = MagicMock()
    fake_source.fetch_new_since = AsyncMock(return_value=[])

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )
    )

    with patch(
        "paper_ingestion.pipelines.auto_fetch.get_source_class",
        return_value=lambda *a, **kw: fake_source,
    ):
        await af.run_auto_pipeline(app)

    fake_source.fetch_new_since.assert_awaited_once()
    topics_arg = fake_source.fetch_new_since.await_args.args[1]
    assert topics_arg == [TopicRef(id=9, name="RL", query_terms=["reinforcement learning"])]


@pytest.mark.asyncio
async def test_subscriber_gets_library_row_via_fan_out():
    """Integration seam: subscribed user → fan_out_to_topic_users → user_library INSERT.

    Uses a mock connection that simulates user_topic_subscriptions returning
    one subscriber (user 5 for topic 10). Asserts that fan_out_to_topic_users
    issues an INSERT INTO user_library with added_via='auto_fetch_topic_match'.
    """
    conn = AsyncMock()
    # list_users_with_topic reads user_topic_subscriptions — return user 5.
    conn.fetch = AsyncMock(return_value=[{"user_id": 5}])
    conn.fetchval = AsyncMock(return_value=1)

    users = await list_users_with_topic(conn, topic_id=10)
    assert users == [5]

    count = await fan_out_to_topic_users(conn, paper_id=99, topic_ids=[10])
    assert count == 1

    conn.fetchval.assert_awaited_once_with(
        "SELECT research.fan_out_library_v1($1::integer[], $2)",
        [5],
        99,
    )


@pytest.mark.asyncio
async def test_run_auto_pipeline_end_to_end_with_subscriber(monkeypatch):
    """End-to-end: run_auto_pipeline with a real subscriber fans out to user_library.

    Patches the verified-public upsert and fan-out to verify that the pipeline
    calls fan-out with the correct paper_id and topic_ids.
    """
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [{"id": 1, "source_type": "arxiv", "enabled": True, "config": {}, "display_order": 1}],
            [{"id": 10, "name": "diffusion"}],
            [],  # to_download
            [],  # to_process
        ]
    )

    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)

    fake_source = MagicMock()
    fake_source.fetch_new_since = AsyncMock(return_value=[_make_paper(0)])

    fanout_args: list[tuple[int, list[int]]] = []

    async def fake_fanout(conn_arg, *, paper_id, topic_ids):
        fanout_args.append((paper_id, list(topic_ids)))
        return 1  # one subscriber received the paper

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )
    )

    with (
        patch(
            "paper_ingestion.pipelines.auto_fetch.get_source_class",
            return_value=lambda *a, **kw: fake_source,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.upsert_verified_public_paper",
            side_effect=lambda conn_arg, paper, **kw: {"id": 42, "is_insert": True},
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.fan_out_to_topic_users",
            side_effect=fake_fanout,
        ),
    ):
        await af.run_auto_pipeline(app)

    assert len(fanout_args) == 1
    paper_id, topic_ids = fanout_args[0]
    assert paper_id == 42
    assert topic_ids == [10]


@pytest.mark.asyncio
async def test_discover_and_save_polls_fetch_new_since_per_topic(monkeypatch):
    """With two real topics plus the zero-configured-topics fallback pair
    ``(None, "machine learning")``, ``_discover_and_save`` calls
    ``fetch_new_since`` once per pair — including the fallback, restoring
    discovery for topic-less installs — but fans out only the two real
    topics (no subscribed topic exists to target for the fallback), and
    building the fallback's placeholder ``TopicRef`` never raises.
    """
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [{"id": 1, "source_type": "arxiv", "enabled": True, "config": {}, "display_order": 1}],
            [
                {"id": 11, "name": "diffusion"},
                {"id": 12, "name": "graphs"},
                {"name": "machine learning"},  # no "id" -> _resolve_topic_pairs yields (None, ...)
            ],
            [],  # to_download
            [],  # to_process
        ]
    )

    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)

    async def fake_fetch_new_since(since, topics, limit=100, user_id=None):
        # One paper per call; distinct external_id per topic so upsert calls
        # are individually traceable back to their triggering topic.
        return [_make_paper(topics[0].id)]

    fake_source = MagicMock()
    fake_source.fetch_new_since = AsyncMock(side_effect=fake_fetch_new_since)

    upsert_count = 0

    async def fake_upsert(conn_arg, paper, **kw):
        nonlocal upsert_count
        upsert_count += 1
        return {"id": 200 + upsert_count, "is_insert": True}

    fanout_calls: list[tuple[int, list[int]]] = []

    async def fake_fanout(conn_arg, *, paper_id, topic_ids):
        fanout_calls.append((paper_id, list(topic_ids)))
        return len(topic_ids)

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )
    )

    with (
        patch(
            "paper_ingestion.pipelines.auto_fetch.get_source_class",
            return_value=lambda *a, **kw: fake_source,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.upsert_verified_public_paper",
            side_effect=fake_upsert,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.fan_out_to_topic_users", side_effect=fake_fanout
        ),
    ):
        await af.run_auto_pipeline(app)

    # One fetch_new_since call per pair — including the topic-less fallback
    # (never a single fan-out-blind blast, and the fallback is no longer
    # silently skipped).
    assert fake_source.fetch_new_since.await_count == 3, (
        f"expected one call per pair (incl. fallback), got {fake_source.fetch_new_since.await_count}"
    )
    called_topic_ids = sorted(
        call.args[1][0].id for call in fake_source.fetch_new_since.await_args_list
    )
    # The fallback's placeholder id (af._DEFAULT_QUERY_TOPIC_ID) builds a
    # valid TopicRef -- no ValidationError -- and is distinct from real ids.
    assert called_topic_ids == sorted([11, 12, af._DEFAULT_QUERY_TOPIC_ID])

    # Each REAL topic's paper is fanned out with THAT topic's id (never
    # blended); the fallback pair has no subscribed topic and is not fanned out.
    assert len(fanout_calls) == 2
    assert sorted(topic_ids[0] for _paper_id, topic_ids in fanout_calls) == [11, 12]


@pytest.mark.asyncio
async def test_discover_and_save_passes_exact_pool_to_source_constructor():
    """Persistent source limiters receive the shared pool; constructor failures are not hidden."""
    conn = AsyncMock()
    pool = MagicMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire)
    captured: dict[str, object] = {}

    class StrictSource:
        def __init__(self, config, http_client, *, db_pool):
            captured.update(config=config, http_client=http_client, db_pool=db_pool)

        async def fetch_new_since(self, *_args, **_kwargs):
            return []

    app = SimpleNamespace(state=SimpleNamespace(http_client=MagicMock()))
    rows = [{"id": 9, "source_type": "arxiv", "enabled": True, "config": {}}]

    with patch("paper_ingestion.pipelines.auto_fetch.get_source_class", return_value=StrictSource):
        await af._discover_and_save(app, pool, rows, [(12, "graphs", [])])

    assert captured["db_pool"] is pool


@pytest.mark.asyncio
async def test_discover_and_save_drains_every_id_a_promotion_recorded():
    """Discovery drains the collector once per recorded id, after the connection is released.

    Unlike the pulse deck loop, this loop hands every promotion the same
    run-wide collector rather than a per-item list merged on success, so an id
    a promotion has recorded is drained even when that promotion then fails.
    Both fakes here therefore record before failing, which is where the pulse
    sibling's per-card list makes the two loops differ. What keeps a rolled-back
    promotion from recording anything at all is
    ``upsert_verified_public_paper`` appending only after its own transaction
    block ends, which is pinned against the real function in
    tests/test_pdf_workflow.py.

    Verified: paper_ingestion/pipelines/auto_fetch.py:114-157 (one collector for
    the whole loop, filled inside the connection block and drained after it)
    """
    order: list[object] = []
    conn = AsyncMock()
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)

    async def _release(*_args) -> bool:
        order.append("connection released")
        return False

    ctx.__aexit__ = AsyncMock(side_effect=_release)
    pool.acquire = MagicMock(return_value=ctx)

    committed, rolled_back = _make_paper(0), _make_paper(1)
    fake_source = MagicMock()
    fake_source.fetch_new_since = AsyncMock(return_value=[committed, rolled_back])

    async def fake_upsert(conn_arg, paper, *, discarded_content_ids=None, **kwargs):
        # Both promotions record a discard; only the second then fails.
        discarded_content_ids.append(111 if paper is committed else 222)
        if paper is rolled_back:
            raise RuntimeError("promotion failed after it recorded an id")
        return {"id": 111, "is_insert": False}

    async def _record_reclaim(paper_id, _pool) -> None:
        order.append(paper_id)

    reclaimed = AsyncMock(side_effect=_record_reclaim)
    app = SimpleNamespace(state=SimpleNamespace(http_client=MagicMock()))

    with (
        patch(
            "paper_ingestion.pipelines.auto_fetch.get_source_class",
            return_value=lambda *a, **kw: fake_source,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.upsert_verified_public_paper",
            side_effect=fake_upsert,
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.fan_out_to_topic_users",
            AsyncMock(return_value=1),
        ),
        patch(
            "paper_ingestion.pipelines.auto_fetch.reclaim_discarded_paper_content",
            reclaimed,
        ),
    ):
        await af._discover_and_save(
            app,
            pool,
            [{"id": 1, "source_type": "arxiv", "enabled": True, "config": {}}],
            [(11, "diffusion", [])],
        )

    assert reclaimed.await_args_list == [call(111, pool), call(222, pool)], (
        "every recorded id must be drained once, in the order it was recorded; "
        f"got {reclaimed.await_args_list}"
    )
    assert order == ["connection released", 111, 222], (
        f"reclaiming must not hold a pool slot: {order}"
    )
