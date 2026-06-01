"""auto_fetch fans out a canonical paper into N user_library rows.

Approach: patch ``upsert_paper`` and ``fan_out_to_topic_users`` and assert
the wiring (one canonical insert per result, one fan-out call per topic
per result).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.library import fan_out_to_topic_users, list_users_with_topic
from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.pipelines import auto_fetch as af


# Keep local: auto-fetch-specific paper (discovery_origin/citation_count/metadata kwargs) not in pulse_helpers.make_pulse_paper.
def _make_paper(idx: int = 0) -> PaperCreate:
    return PaperCreate(
        external_id=f"arxiv:1000.0000{idx}",
        source_type=SourceType.ARXIV,
        title=f"Paper {idx}",
        authors=["Alice"],
        abstract="abs",
        url="https://example.org",
        pdf_url=None,
        citation_count=0,
        metadata={},
        discovery_origin="user_initiated",
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
    fake_source.search = AsyncMock(return_value=[_make_paper(i) for i in range(3)])

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
        patch("paper_ingestion.pipelines.auto_fetch.upsert_paper", side_effect=fake_upsert),
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
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    users = await list_users_with_topic(conn, topic_id=10)
    assert users == [5]

    count = await fan_out_to_topic_users(conn, paper_id=99, topic_ids=[10])
    assert count == 1

    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO user_library" in sql
    assert "auto_fetch_topic_match" in sql
    assert "ON CONFLICT (user_id, paper_id) DO NOTHING" in sql


@pytest.mark.asyncio
async def test_run_auto_pipeline_end_to_end_with_subscriber(monkeypatch):
    """End-to-end: run_auto_pipeline with a real subscriber fans out to user_library.

    Patches upsert_paper and fan_out_to_topic_users to verify that the pipeline
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
    fake_source.search = AsyncMock(return_value=[_make_paper(0)])

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
            "paper_ingestion.pipelines.auto_fetch.upsert_paper",
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
