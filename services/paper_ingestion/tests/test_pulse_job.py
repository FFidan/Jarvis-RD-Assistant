"""Tests for app.pulse.job.run_pulse — 7-step orchestration.

TDD: tests written before implementation.
"""

import asyncio
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
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
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
    # B4: LLM timeout is degraded (not fatal) — last_error stays None
    assert stats["last_error"] is None
    assert stats.get("degraded_reason") is not None
    assert "timed out" in str(stats["degraded_reason"]).lower()


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
    # B4: stage2 exception with fallback is degraded, not fatal
    assert stats["last_error"] is None
    assert stats.get("degraded_reason") is not None
    assert "stage2 exploded" in stats["degraded_reason"]


@pytest.mark.asyncio
async def test_upsert_persist_atomic_on_failure(patch_pipeline):
    """upsert_paper and persist_deck run inside a single conn.transaction().

    Code-path verification: the job acquires exactly one connection, opens
    exactly one transaction on it, and passes that *same* connection to both
    upsert_paper and persist_deck.  If persist_deck raises, the transaction
    context manager's __aexit__ is invoked (rollback path) and the error is
    recorded in stats — no orphaned papers are left uncommitted.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.pulse.job import run_pulse

    # Build a pool whose acquire() yields a controlled connection so we can
    # assert exactly which connection is handed to each collaborator.
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    # Simulate rollback: __aexit__ returns False (exception propagates)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    # Capture which connection upsert_paper and persist_deck are called with
    upsert_conns: list = []
    persist_conns: list = []

    async def recording_upsert(c, paper, **_kw):
        upsert_conns.append(c)
        return {"id": 1, "is_insert": True}

    async def boom_persist(db_pool, deck_date, cards, stats, conn=None, **_kw):
        persist_conns.append(conn)
        raise RuntimeError("persist_deck exploded")

    patch_pipeline["mocks"]["upsert_paper"].side_effect = recording_upsert
    patch_pipeline["mocks"]["persist_deck"].side_effect = boom_persist

    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # 1. A single connection was acquired for the entire persist step
    pool.acquire.assert_called_once()

    # 2. A single transaction was opened on that connection
    conn.transaction.assert_called_once()

    # 3. The transaction context manager was entered and exited (rollback path)
    txn_cm.__aenter__.assert_awaited_once()
    txn_cm.__aexit__.assert_awaited_once()

    # 4. upsert_paper was called with the shared connection
    deck_size = len(patch_pipeline["deck"])
    assert len(upsert_conns) == deck_size
    assert all(c is conn for c in upsert_conns), "upsert_paper must use the shared conn"

    # 5. persist_deck was called with conn= keyword (shared connection path)
    assert len(persist_conns) == 1
    assert persist_conns[0] is conn, "persist_deck must receive the shared conn"

    # 6. The failure is captured in stats (pipeline does not raise)
    assert stats["last_error"] is not None
    assert "persist_deck exploded" in stats["last_error"]


# ---------------------------------------------------------------------------
# B4 — degraded_reason vs last_error distinction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_timeout_sets_degraded_reason_not_last_error(patch_pipeline):
    """LLM timeout → degraded_reason populated, last_error stays None, deck produced."""
    from app.pulse.job import run_pulse

    async def raise_timeout(*_a, **_kw):
        raise TimeoutError()

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = raise_timeout

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

    # B4: timeout is degraded, NOT fatal — deck was still produced
    assert stats["last_error"] is None, "last_error must be None for a degraded (timeout) run"
    assert stats.get("degraded_reason") is not None
    assert "LLM scoring timed out" in stats["degraded_reason"]
    assert "embedding-only fallback" in stats["degraded_reason"]
    # Deck was produced
    patch_pipeline["mocks"]["persist_deck"].assert_awaited_once()
    persist_call = patch_pipeline["mocks"]["persist_deck"].await_args
    # degraded_reason forwarded to persist_deck
    dr = persist_call.kwargs.get("degraded_reason")
    assert dr is not None
    assert "LLM scoring timed out" in dr


@pytest.mark.asyncio
async def test_stage2_exception_sets_degraded_reason_not_last_error(patch_pipeline):
    """Stage2 exception with fallback → degraded_reason set, last_error is None."""
    from app.pulse.job import run_pulse

    async def boom(*_a, **_kw):
        raise RuntimeError("model overloaded")

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = boom

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # B4: stage2 exception with fallback is degraded, not fatal
    assert stats["last_error"] is None
    assert stats.get("degraded_reason") is not None
    assert "stage2 error" in stats["degraded_reason"]
    assert "model overloaded" in stats["degraded_reason"]
    # Deck was still produced
    patch_pipeline["mocks"]["persist_deck"].assert_awaited_once()


@pytest.mark.asyncio
async def test_stage1_exception_sets_last_error_not_degraded(patch_pipeline):
    """Stage1 failure with empty output → last_error set, degraded_reason stays None."""
    from app.pulse.job import run_pulse

    async def boom(*_a, **_kw):
        raise RuntimeError("embedding service down")

    patch_pipeline["mocks"]["stage1_embedding_filter"].side_effect = boom

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # Stage1 failure: deck proceeds with empty stage1_out, so it IS recorded as last_error
    # (the overall pipeline degrades but no dedicated degraded_reason is set here)
    assert stats["last_error"] is not None
    assert "stage1" in stats["last_error"]
    assert stats.get("degraded_reason") is None


@pytest.mark.asyncio
async def test_happy_path_both_null(patch_pipeline):
    """A clean run has last_error=None and no degraded_reason."""
    from app.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    assert stats["last_error"] is None
    assert stats.get("degraded_reason") is None


# ---------------------------------------------------------------------------
# B1 — ctx progress reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctx_progress_reported_in_happy_path(patch_pipeline):
    """When a JobContext is supplied, progress checkpoints are reported."""
    from unittest.mock import AsyncMock

    from app.pulse.job import run_pulse

    ctx = MagicMock()
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(return_value=False)

    pool, _conn = _make_pool_and_conn()
    await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC), ctx=ctx)

    # At minimum: 0.05, 0.20, 0.30, 0.85, 0.90, 0.93, 1.00
    progress_values = [call.args[0] for call in ctx.update_progress.await_args_list]
    assert 0.05 in progress_values
    assert 1.0 in progress_values
    assert len(progress_values) >= 7


@pytest.mark.asyncio
async def test_ctx_cancellation_is_respected(patch_pipeline):
    """When is_cancelled() returns True, CancelledError is raised."""
    from unittest.mock import AsyncMock

    from app.pulse.job import run_pulse

    # Cancel after profile loaded (is_cancelled called at 0.20 boundary)
    call_count = 0

    async def _is_cancelled():
        nonlocal call_count
        call_count += 1
        # First call (at 0.05 boundary) returns False, second call returns True
        return call_count > 1

    ctx = MagicMock()
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = _is_cancelled

    pool, _conn = _make_pool_and_conn()
    with pytest.raises(asyncio.CancelledError):
        await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC), ctx=ctx)
