"""Tests for app.pulse.job.run_pulse — 7-step orchestration.

TDD: tests written before implementation.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models import PaperCreate, SourceType, TopicRef
from app.pulse.profile import UserProfile
from app.pulse.scoring import ScoredCandidate

from tests.conftest import _make_pool_and_conn


def _make_profile(deck_size: int = 10, stage2_top_k: int = 30) -> UserProfile:
    return UserProfile(
        topics=[TopicRef(id=1, name="ml", query_terms=["ML"])],
        tracked_author_ids=[],
        library_centroid=None,
        weights={"embedding": 0.35, "topic": 0.25, "recency": 0.15, "author_bonus": 0.25},
        deck_size=deck_size,
        stage2_top_k=stage2_top_k,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )


def _paper(idx: int) -> PaperCreate:
    return PaperCreate(
        external_id=f"arxiv:{idx:04d}",
        source_type=SourceType.ARXIV,
        title=f"Paper {idx}",
        authors=["Author A"],
        abstract="Abstract",
        url=f"https://arxiv.org/abs/{idx:04d}",
    )


def _scored(idx: int, score: float = 0.8) -> ScoredCandidate:
    return ScoredCandidate(
        paper=_paper(idx),
        signals={"embedding": score, "topic": 0.5, "recency": 0.9, "author_bonus": 0.0},
        llm_relevance=7,
        llm_novelty=5,
        reasoning="relevant",
        final_score=score,
    )


@pytest.fixture
def patch_pipeline():
    """Patch all collaborators of run_pulse with AsyncMock stand-ins."""
    profile = _make_profile()
    candidates = [_paper(i) for i in range(20)]
    stage1_out = [_scored(i) for i in range(15)]
    stage2_out = [_scored(i) for i in range(15)]
    stage3_out = [_scored(i) for i in range(15)]
    deck = stage3_out[:10]

    patches = {
        "load_profile": AsyncMock(return_value=profile),
        "discover_candidates": AsyncMock(return_value=candidates),
        "stage1_embedding_filter": AsyncMock(return_value=stage1_out),
        "stage2_llm_rerank": AsyncMock(return_value=stage2_out),
        "stage3_combine": AsyncMock(return_value=stage3_out),
        "assemble_deck": AsyncMock(return_value=deck),
        "upsert_paper": AsyncMock(return_value={"id": 1, "is_insert": True}),
        "persist_deck": AsyncMock(return_value=42),
    }

    context = {
        "profile": profile,
        "candidates": candidates,
        "stage1": stage1_out,
        "stage2": stage2_out,
        "stage3": stage3_out,
        "deck": deck,
        "mocks": patches,
    }

    with (
        patch("app.pulse.job.load_profile", patches["load_profile"]),
        patch("app.pulse.job.discover_candidates", patches["discover_candidates"]),
        patch("app.pulse.job.stage1_embedding_filter", patches["stage1_embedding_filter"]),
        patch("app.pulse.job.stage2_llm_rerank", patches["stage2_llm_rerank"]),
        patch("app.pulse.job.stage3_combine", patches["stage3_combine"]),
        patch("app.pulse.job.assemble_deck", patches["assemble_deck"]),
        patch("app.pulse.job.upsert_paper", patches["upsert_paper"]),
        patch("app.pulse.job.persist_deck", patches["persist_deck"]),
    ):
        yield context


@pytest.mark.asyncio
async def test_happy_path_end_to_end(patch_pipeline):
    from app.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()
    http_client = MagicMock()
    embedder = MagicMock()
    now = datetime(2026, 4, 10, 4, 0, tzinfo=UTC)

    stats = await run_pulse(pool, http_client, embedder, now=now)

    mocks = patch_pipeline["mocks"]
    mocks["load_profile"].assert_awaited_once()
    mocks["discover_candidates"].assert_awaited_once()
    mocks["stage1_embedding_filter"].assert_awaited_once()
    mocks["stage2_llm_rerank"].assert_awaited_once()
    mocks["stage3_combine"].assert_awaited_once()
    mocks["assemble_deck"].assert_awaited_once()
    assert mocks["upsert_paper"].await_count == len(patch_pipeline["deck"])
    mocks["persist_deck"].assert_awaited_once()

    persist_call = mocks["persist_deck"].await_args
    assert persist_call.kwargs.get("deck_date") == now.date() or (
        len(persist_call.args) >= 2 and persist_call.args[1] == now.date()
    )

    assert stats["candidate_count"] == len(patch_pipeline["candidates"])
    assert stats["stage1_survivors"] == len(patch_pipeline["stage1"])
    assert stats["stage2_scored"] == len(patch_pipeline["stage2"])
    assert stats["duration_s"] >= 0
    assert stats["last_error"] is None


@pytest.mark.asyncio
async def test_empty_discovery_produces_empty_deck(patch_pipeline):
    from app.pulse.job import run_pulse

    patch_pipeline["mocks"]["discover_candidates"].return_value = []
    patch_pipeline["mocks"]["stage1_embedding_filter"].return_value = []
    patch_pipeline["mocks"]["stage2_llm_rerank"].return_value = []
    patch_pipeline["mocks"]["stage3_combine"].return_value = []
    patch_pipeline["mocks"]["assemble_deck"].return_value = []

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    assert stats["candidate_count"] == 0
    assert stats["stage1_survivors"] == 0
    assert stats["last_error"] is None
    patch_pipeline["mocks"]["persist_deck"].assert_awaited_once()
    assert patch_pipeline["mocks"]["upsert_paper"].await_count == 0


@pytest.mark.asyncio
async def test_llm_timeout_falls_back_to_stage1(patch_pipeline):
    from app.pulse.job import run_pulse

    async def raise_timeout(*_a, **_kw):
        raise TimeoutError()

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = raise_timeout

    # stage3 is invoked on the fallback (stage1 survivors with None LLM fields)
    async def stage3_impl(stage2_out, weights):
        for sc in stage2_out:
            sc.final_score = 0.5
        return stage2_out

    patch_pipeline["mocks"]["stage3_combine"].side_effect = stage3_impl

    async def assemble_impl(scored, size):
        return scored[:size]

    patch_pipeline["mocks"]["assemble_deck"].side_effect = assemble_impl

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    persist_call = patch_pipeline["mocks"]["persist_deck"].await_args
    cards = persist_call.kwargs.get("cards") or persist_call.args[2]
    assert len(cards) > 0
    for c in cards:
        assert c.llm_relevance is None
        assert c.llm_novelty is None
        assert c.reasoning is None
    assert stats["last_error"] is not None
    assert "timeout" in str(stats["last_error"]).lower()


async def _echo(x):
    return x


@pytest.mark.asyncio
async def test_upsert_happens_before_persist_deck(patch_pipeline):
    from app.pulse.job import run_pulse

    order: list[str] = []

    async def rec_upsert(*_a, **_kw):
        order.append("upsert")
        return {"id": 1, "is_insert": True}

    async def rec_persist(*_a, **_kw):
        order.append("persist")
        return 42

    patch_pipeline["mocks"]["upsert_paper"].side_effect = rec_upsert
    patch_pipeline["mocks"]["persist_deck"].side_effect = rec_persist

    pool, _conn = _make_pool_and_conn()
    await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # All upserts should come before persist_deck
    assert "persist" in order
    persist_idx = order.index("persist")
    assert all(x == "upsert" for x in order[:persist_idx])
    assert order[persist_idx] == "persist"


@pytest.mark.asyncio
async def test_stats_includes_last_error_on_partial_failure(patch_pipeline):
    from app.pulse.job import run_pulse

    async def boom(*_a, **_kw):
        raise RuntimeError("stage2 exploded")

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = boom

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # Deck should still be produced (from stage1 fallback)
    patch_pipeline["mocks"]["persist_deck"].assert_awaited_once()
    assert stats["last_error"] is not None
    assert "stage2 exploded" in stats["last_error"]
