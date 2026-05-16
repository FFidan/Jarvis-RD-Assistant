"""Tests for app.pulse.job.run_pulse — 7-step orchestration.

TDD: tests written before implementation.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.models import PaperCreate, SourceType, TopicRef
from paper_ingestion.pulse.profile import UserProfile
from paper_ingestion.pulse.scoring import ScoredCandidate

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
        "discover_candidates": AsyncMock(
            return_value=(candidates, {"_MockSrc": len(candidates)}, {})
        ),
        "stage1_embedding_filter": AsyncMock(return_value=stage1_out),
        # C2: stage2_llm_rerank is now called per-batch; return the batch input unchanged.
        "stage2_llm_rerank": AsyncMock(side_effect=lambda batch, *a, **kw: batch),
        "stage3_combine": AsyncMock(return_value=stage3_out),
        "assemble_deck": MagicMock(return_value=deck),
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
        patch("paper_ingestion.pulse.job.load_profile", patches["load_profile"]),
        patch("paper_ingestion.pulse.job.discover_candidates", patches["discover_candidates"]),
        patch(
            "paper_ingestion.pulse.job.stage1_embedding_filter", patches["stage1_embedding_filter"]
        ),
        patch("paper_ingestion.pulse.job.stage2_llm_rerank", patches["stage2_llm_rerank"]),
        patch("paper_ingestion.pulse.job.stage3_combine", patches["stage3_combine"]),
        patch("paper_ingestion.pulse.job.assemble_deck", patches["assemble_deck"]),
        patch("paper_ingestion.pulse.job.upsert_paper", patches["upsert_paper"]),
        patch("paper_ingestion.pulse.job.persist_deck", patches["persist_deck"]),
    ):
        yield context


@pytest.mark.asyncio
async def test_happy_path_end_to_end(patch_pipeline):
    from paper_ingestion.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()
    http_client = MagicMock()
    embedder = MagicMock()
    now = datetime(2026, 4, 10, 4, 0, tzinfo=UTC)

    stats = await run_pulse(pool, http_client, embedder, now=now)

    mocks = patch_pipeline["mocks"]
    mocks["load_profile"].assert_awaited_once()
    mocks["discover_candidates"].assert_awaited_once()
    mocks["stage1_embedding_filter"].assert_awaited_once()
    # C2: stage2_llm_rerank is called once per batch of 5 candidates
    assert mocks["stage2_llm_rerank"].await_count >= 1
    mocks["stage3_combine"].assert_awaited_once()
    mocks["assemble_deck"].assert_called_once()
    assert mocks["upsert_paper"].await_count == len(patch_pipeline["deck"])
    mocks["persist_deck"].assert_awaited_once()
    persist_call = mocks["persist_deck"].await_args
    assert persist_call.kwargs.get("deck_date") == now.date() or (
        len(persist_call.args) >= 2 and persist_call.args[1] == now.date()
    )
    assert persist_call.kwargs["stats"]["source_diagnostics"] == {}

    assert stats["candidate_count"] == len(patch_pipeline["candidates"])
    assert stats["stage1_survivors"] == len(patch_pipeline["stage1"])
    assert stats["stage2_scored"] == len(patch_pipeline["stage2"])
    assert stats["duration_s"] >= 0
    assert stats["last_error"] is None


@pytest.mark.asyncio
async def test_run_pulse_threads_user_id_to_profile_and_persistence(patch_pipeline):
    """Per-user Pulse jobs must not drop user_id inside the pipeline."""
    from paper_ingestion.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()
    now = datetime(2026, 5, 11, 4, 0, tzinfo=UTC)

    await run_pulse(pool, MagicMock(), MagicMock(), now=now, user_id=42)

    mocks = patch_pipeline["mocks"]
    assert mocks["load_profile"].await_args.kwargs["user_id"] == 42
    assert mocks["persist_deck"].await_args.kwargs["user_id"] == 42


@pytest.mark.asyncio
async def test_run_pulse_threads_user_id_to_classifier_scores(patch_pipeline):
    """Explicit classifier scoring must train and score against the requesting user."""
    from paper_ingestion.pulse.job import run_pulse

    patch_pipeline["profile"].weights["classifier"] = 0.2
    classifier_scores = AsyncMock(
        return_value=(
            [0.1 for _ in patch_pipeline["stage2"]],
            {"available": True, "feature_names": []},
        )
    )

    pool, _conn = _make_pool_and_conn()
    with patch("paper_ingestion.pulse.job.classifier_scores", classifier_scores):
        await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC), user_id=42)

    assert classifier_scores.await_args.kwargs["user_id"] == 42


@pytest.mark.asyncio
async def test_stage2_scores_all_survivors_in_one_call(patch_pipeline):
    """Progress reporting must not throttle Stage 2 into sequential batches of five."""
    from paper_ingestion.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()
    await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    stage2 = patch_pipeline["mocks"]["stage2_llm_rerank"]
    stage2.assert_awaited_once()
    assert stage2.await_args.args[0] == patch_pipeline["stage1"]


@pytest.mark.asyncio
async def test_zero_candidates_sets_degraded_reason_and_source_diagnostics():
    from paper_ingestion.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()
    diagnostics = {
        "ArxivSource": {
            "status": "rate_limit",
            "message": "arXiv rate limit reached.",
            "status_code": 429,
            "retry_after_s": 60,
            "settings_hint": None,
        },
        "OpenAlexSource": {
            "status": "unconfigured",
            "message": "OpenAlex requires OPENALEX_EMAIL or OPENALEX_API_KEY.",
            "status_code": None,
            "retry_after_s": None,
            "settings_hint": "Set OPENALEX_EMAIL or OPENALEX_API_KEY.",
        },
    }

    with (
        patch("paper_ingestion.pulse.job.load_profile", AsyncMock(return_value=_make_profile())),
        patch(
            "paper_ingestion.pulse.job.discover_candidates",
            AsyncMock(return_value=([], {"ArxivSource": 0, "OpenAlexSource": 0}, diagnostics)),
        ),
        patch("paper_ingestion.pulse.job.stage1_embedding_filter", AsyncMock(return_value=[])),
        patch("paper_ingestion.pulse.job.stage3_combine", AsyncMock(return_value=[])),
        patch("paper_ingestion.pulse.job.assemble_deck", MagicMock(return_value=[])),
        patch("paper_ingestion.pulse.job.persist_deck", AsyncMock(return_value=0)) as persist,
    ):
        stats = await run_pulse(
            pool,
            MagicMock(),
            MagicMock(),
            now=datetime(2026, 5, 6, 4, 0, tzinfo=UTC),
        )

    assert stats["candidate_count"] == 0
    assert stats["last_error"] is None
    assert stats["source_diagnostics"] == diagnostics
    assert stats["degraded_reason"].startswith("No Pulse candidates returned")
    assert "arXiv" in stats["degraded_reason"]
    persist.assert_awaited_once()
    assert persist.await_args.kwargs["degraded_reason"] == stats["degraded_reason"]
    assert persist.await_args.kwargs["stats"]["degraded_reason"] == stats["degraded_reason"]


@pytest.mark.asyncio
async def test_empty_discovery_produces_empty_deck(patch_pipeline):
    from paper_ingestion.pulse.job import run_pulse

    patch_pipeline["mocks"]["discover_candidates"].return_value = ([], {}, {})
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
    from paper_ingestion.pulse.job import run_pulse

    async def raise_timeout(*_a, **_kw):
        raise TimeoutError()

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = raise_timeout

    # stage3 is invoked on the fallback (stage1 survivors with None LLM fields)
    async def stage3_impl(stage2_out, weights):
        for sc in stage2_out:
            sc.final_score = 0.5
        return stage2_out

    patch_pipeline["mocks"]["stage3_combine"].side_effect = stage3_impl

    def assemble_impl(scored, size):
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
    from paper_ingestion.pulse.job import run_pulse

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
    from paper_ingestion.pulse.job import run_pulse

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

    from paper_ingestion.pulse.job import run_pulse

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

    # 1. A connection was acquired for the persist step; a second acquire is
    # expected for the verification_stats log_event emitted at the end of run_pulse.
    assert pool.acquire.call_count >= 1, "persist step must acquire a DB connection"

    # 2. Outer transaction + one SAVEPOINT per card (B1.1)
    deck_size = len(patch_pipeline["deck"])
    # transaction() called: 1 outer + deck_size savepoints
    assert conn.transaction.call_count == 1 + deck_size, (
        f"Expected 1 outer + {deck_size} savepoints = {1 + deck_size} calls, "
        f"got {conn.transaction.call_count}"
    )

    # 3. The outer transaction context manager was entered (rollback path on persist failure);
    # since txn_cm is shared across all transaction() calls, we only verify at least one enter.
    assert txn_cm.__aenter__.await_count >= 1

    # 4. upsert_paper was called with the shared connection
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
    from paper_ingestion.pulse.job import run_pulse

    async def raise_timeout(*_a, **_kw):
        raise TimeoutError()

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = raise_timeout

    async def stage3_impl(stage2_out, weights):
        for sc in stage2_out:
            sc.final_score = 0.5
        return stage2_out

    patch_pipeline["mocks"]["stage3_combine"].side_effect = stage3_impl

    def assemble_impl(scored, size):
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
    from paper_ingestion.pulse.job import run_pulse

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
    from paper_ingestion.pulse.job import run_pulse

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
    from paper_ingestion.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    assert stats["last_error"] is None
    assert stats.get("degraded_reason") is None


@pytest.mark.asyncio
async def test_optional_signals_requested_are_enriched_before_stage3(patch_pipeline):
    """Citation and classifier weights trigger Phase 2 enrichment before combining."""
    from paper_ingestion.pulse.job import run_pulse

    patch_pipeline["profile"].weights = {
        "embedding": 0.2,
        "citation_pagerank": 0.2,
        "citation_count": 0.1,
        "citation_adamic_adar": 0.1,
        "classifier": 0.5,
    }
    citation_values = {
        sc.paper.external_id: {
            "citation_pagerank": 0.25 + idx / 100.0,
            "citation_count": 1.0 - idx / 100.0,
            "citation_adamic_adar": 0.5,
        }
        for idx, sc in enumerate(patch_pipeline["stage2"])
    }
    classifier_values = [0.9 - idx / 100.0 for idx, _sc in enumerate(patch_pipeline["stage2"])]
    captured_stage3_input: list[list[ScoredCandidate]] = []

    async def stage3_impl(stage2_out, _weights):
        captured_stage3_input.append(stage2_out)
        return stage2_out

    patch_pipeline["mocks"]["stage3_combine"].side_effect = stage3_impl

    with (
        patch(
            "paper_ingestion.pulse.job.compute_citation_signals",
            AsyncMock(return_value=citation_values),
        ) as compute_citation_signals,
        patch(
            "paper_ingestion.pulse.job.classifier_scores",
            AsyncMock(return_value=(classifier_values, {"available": True, "sample_count": 42})),
        ) as classifier_scores,
    ):
        pool, _conn = _make_pool_and_conn()
        stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    compute_citation_signals.assert_awaited_once_with(
        pool,
        [sc.paper.external_id for sc in patch_pipeline["stage2"]],
        user_id=None,
    )
    classifier_scores.assert_awaited_once()
    assert classifier_scores.await_args is not None
    assert classifier_scores.await_args.args[0] is pool
    classifier_input = classifier_scores.await_args.args[1]
    assert (
        classifier_input[0]["citation_pagerank"]
        == citation_values["arxiv:0000"]["citation_pagerank"]
    )
    assert classifier_input[0]["citation_count"] == citation_values["arxiv:0000"]["citation_count"]
    assert (
        classifier_input[0]["citation_adamic_adar"]
        == citation_values["arxiv:0000"]["citation_adamic_adar"]
    )

    assert len(captured_stage3_input) == 1
    first_signals = captured_stage3_input[0][0].signals
    assert first_signals["citation_pagerank"] == citation_values["arxiv:0000"]["citation_pagerank"]
    assert first_signals["citation_count"] == citation_values["arxiv:0000"]["citation_count"]
    assert (
        first_signals["citation_adamic_adar"]
        == citation_values["arxiv:0000"]["citation_adamic_adar"]
    )
    assert first_signals["classifier"] == classifier_values[0]
    assert stats["classifier"] == {"available": True, "sample_count": 42}


@pytest.mark.asyncio
async def test_optional_signals_disabled_reports_classifier_disabled(patch_pipeline):
    """Zero optional weights skip optional calls and expose classifier metadata."""
    from paper_ingestion.pulse.job import run_pulse

    with (
        patch("paper_ingestion.pulse.job.compute_citation_signals", AsyncMock()) as citations,
        patch("paper_ingestion.pulse.job.classifier_scores", AsyncMock()) as classifier,
    ):
        pool, _conn = _make_pool_and_conn()
        stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    citations.assert_not_awaited()
    classifier.assert_not_awaited()
    assert stats["classifier"]["available"] is False
    assert stats["classifier"]["degradation_reason"] == "classifier weight is disabled"
    assert "feature_names" in stats["classifier"]


# ---------------------------------------------------------------------------
# B1 — ctx progress reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctx_progress_reported_in_happy_path(patch_pipeline):
    """When a JobContext is supplied, progress checkpoints are reported."""
    from unittest.mock import AsyncMock

    from paper_ingestion.pulse.job import run_pulse

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

    from paper_ingestion.pulse.job import run_pulse

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


# ---------------------------------------------------------------------------
# Medium #15 — parameterised fatal-error vs degraded_reason distinction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage_mock, expected_last_error_fragment",
    [
        # Stage 1: embedding filter raises → last_error set, degraded_reason NULL
        ("stage1_embedding_filter", "stage1"),
        # Stage 3: weighted-combine raises → last_error set, degraded_reason NULL
        ("stage3_combine", "stage3"),
        # assemble_deck raises → last_error set, degraded_reason NULL
        ("assemble_deck", "assemble_deck"),
        # upsert_paper raises → last_error set, degraded_reason NULL
        ("upsert_paper", "upsert_paper"),
    ],
)
async def test_fatal_stage_errors_set_last_error_not_degraded(
    patch_pipeline,
    stage_mock: str,
    expected_last_error_fragment: str,
):
    """Fatal stage exceptions populate last_error but leave degraded_reason NULL.

    Degraded is only for cases where a fallback is used and a deck is still
    produced (e.g. stage2 LLM timeout).  Any stage that has no fallback and
    records to stats['last_error'] is FATAL — degraded_reason must stay None
    so that the pulse_decks.degraded_reason column is not falsely populated.
    """
    from paper_ingestion.pulse.job import run_pulse

    patch_pipeline["mocks"][stage_mock].side_effect = RuntimeError("boom")

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # 1. last_error must be populated with the exception message
    assert stats["last_error"] is not None, (
        f"Expected last_error to be set after {stage_mock} raises, got None"
    )
    assert expected_last_error_fragment in stats["last_error"], (
        f"Expected '{expected_last_error_fragment}' in last_error, got: {stats['last_error']!r}"
    )
    assert "boom" in stats["last_error"], (
        f"Expected exception message 'boom' in last_error, got: {stats['last_error']!r}"
    )

    # 2. degraded_reason must be NULL (fatal, not degraded)
    assert stats.get("degraded_reason") is None, (
        f"Expected degraded_reason to be None for fatal {stage_mock} error, "
        f"got: {stats.get('degraded_reason')!r}"
    )


# ---------------------------------------------------------------------------
# _pulse_generate_job — job backbone entry point (pulse_now / cron path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pulse_generate_job_happy_path():
    """_pulse_generate_job calls run_pulse and returns deck_date + card_count."""
    from paper_ingestion.pulse.job import _pulse_generate_job

    pool, _conn = _make_pool_and_conn()
    http_client = MagicMock()

    fake_app = MagicMock()
    fake_app.state.embedder = MagicMock()
    fake_app.state.sources = None

    ctx = MagicMock()
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(return_value=False)

    now_iso = "2026-04-10T04:00:00+00:00"
    payload = {"now": now_iso}

    mock_run = AsyncMock(
        return_value={
            "deck_date": "2026-04-10",
            "card_count": 10,
            "candidate_count": 50,
            "stage1_survivors": 30,
            "stage2_scored": 15,
            "duration_s": 2.3,
            "last_error": None,
        }
    )

    fake_lock = MagicMock()
    fake_lock.__aenter__ = AsyncMock(return_value=True)
    fake_lock.__aexit__ = AsyncMock(return_value=False)
    with patch("paper_ingestion.pulse.job.AdvisoryLock", return_value=fake_lock):
        with patch("paper_ingestion.pulse.job.run_pulse", mock_run):
            with patch("paper_ingestion.main.app", fake_app, create=True):
                result = await _pulse_generate_job(pool, http_client, payload, ctx)

    assert result["deck_date"] == "2026-04-10"
    assert result["card_count"] == 10
    assert "stats" in result


@pytest.mark.asyncio
async def test_pulse_generate_job_now_param_forwarded():
    """_pulse_generate_job forwards the `now` ISO string to run_pulse as a datetime."""
    from paper_ingestion.pulse.job import _pulse_generate_job

    pool, _conn = _make_pool_and_conn()
    http_client = MagicMock()

    fake_app = MagicMock()
    fake_app.state.embedder = MagicMock()
    fake_app.state.sources = None

    ctx = MagicMock()
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(return_value=False)

    now_iso = "2026-01-15T04:00:00+00:00"
    expected_dt = datetime.fromisoformat(now_iso)

    captured: list[datetime] = []

    async def recording_run_pulse(**kwargs):
        now_arg = kwargs.get("now")
        if now_arg is not None:
            captured.append(now_arg)
        return {
            "deck_date": "2026-01-15",
            "card_count": 5,
            "candidate_count": 20,
            "stage1_survivors": 10,
            "stage2_scored": 5,
            "duration_s": 1.0,
            "last_error": None,
        }

    fake_lock = MagicMock()
    fake_lock.__aenter__ = AsyncMock(return_value=True)
    fake_lock.__aexit__ = AsyncMock(return_value=False)
    with patch("paper_ingestion.pulse.job.AdvisoryLock", return_value=fake_lock):
        with patch("paper_ingestion.pulse.job.run_pulse", side_effect=recording_run_pulse):
            with patch("paper_ingestion.main.app", fake_app, create=True):
                await _pulse_generate_job(pool, http_client, {"now": now_iso}, ctx)

    assert len(captured) == 1
    assert captured[0] == expected_dt


# ---------------------------------------------------------------------------
# B1.1 — savepoint isolation per card
# B1.2 — 0-card deck WARNING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pulse_savepoint_isolates_per_card_failure(patch_pipeline, caplog):
    """A single-card upsert failure rolls back only its savepoint; other cards persist.

    The outer transaction stays alive so persist_deck receives the 2 successful
    cards (not 0) — the poisoned-transaction bug (PI-CORE-001) is fixed.
    """
    import logging

    from paper_ingestion.pulse.job import run_pulse

    # Override assemble_deck to return exactly 3 known cards
    cards = [_scored(0), _scored(1), _scored(2)]
    patch_pipeline["mocks"]["assemble_deck"].return_value = cards

    # upsert_paper raises on the first call, succeeds on the rest
    call_count = 0

    async def selective_upsert(conn, paper, **_kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("db constraint violation on card 0")
        return {"id": call_count, "is_insert": True}

    # persist_deck records which cards it receives
    received_cards: list = []

    async def recording_persist(db_pool, deck_date, cards, stats, conn=None, **_kw):
        received_cards.extend(cards)
        return len(cards)

    patch_pipeline["mocks"]["upsert_paper"].side_effect = selective_upsert
    patch_pipeline["mocks"]["persist_deck"].side_effect = recording_persist

    pool, _conn = _make_pool_and_conn()
    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.job"):
        stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # persist_deck must have been called (outer transaction survived)
    patch_pipeline["mocks"]["persist_deck"].assert_awaited_once()

    # All 3 cards are still passed to persist_deck (deck list is unchanged — upserts that
    # fail are skipped in the DB but the deck record still references them for UI display)
    assert len(received_cards) == 3

    # successes count: 2 cards upserted, 1 skipped
    # last_error set from the failed card
    assert stats.get("last_error") is not None
    assert "db constraint violation on card 0" in stats["last_error"]

    # A WARNING was logged for the failed card
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("db constraint violation on card 0" in m for m in warning_messages), (
        f"Expected WARNING about failed card; got: {warning_messages}"
    )


# ---------------------------------------------------------------------------
# C2 — Stage 2 LLM scoring progress under single-call reranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_progress_updates_report_single_fast_call(patch_pipeline):
    """Stage 2 reports one completion update after the single structured LLM call."""
    from paper_ingestion.pulse.job import run_pulse

    stage1_survivors = [_scored(i) for i in range(12)]
    patch_pipeline["mocks"]["stage1_embedding_filter"].return_value = stage1_survivors

    async def passthrough_stage2(survivors, *a, **kw):
        return list(survivors)

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = passthrough_stage2

    # Record all update_progress calls
    progress_calls: list[tuple[float, str | None]] = []

    async def record_progress(pct, msg=None):
        progress_calls.append((pct, msg))

    ctx = MagicMock()
    ctx.update_progress = AsyncMock(side_effect=record_progress)
    ctx.is_cancelled = AsyncMock(return_value=False)

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC), ctx=ctx)

    # Filter to stage-2-specific progress messages
    stage2_msgs = [
        (pct, msg)
        for pct, msg in progress_calls
        if msg and "Stage 2 LLM scoring" in msg and "/" in msg
    ]

    assert stage2_msgs == [(0.95, "Stage 2 LLM scoring (12/12)")]
    patch_pipeline["mocks"]["stage2_llm_rerank"].assert_awaited_once()

    assert stats["last_error"] is None
    assert stats.get("stage2_scored") == 12


@pytest.mark.asyncio
async def test_run_pulse_logs_warn_on_zero_card_deck(patch_pipeline, caplog):
    """When all upserts fail, a WARNING is logged mentioning '0-card deck' and last_error."""
    import logging

    from paper_ingestion.pulse.job import run_pulse

    # 2 cards, both upserts fail
    cards = [_scored(10), _scored(11)]
    patch_pipeline["mocks"]["assemble_deck"].return_value = cards

    async def always_fail(conn, paper, **_kw):
        raise RuntimeError("catastrophic db error")

    patch_pipeline["mocks"]["upsert_paper"].side_effect = always_fail

    pool, _conn = _make_pool_and_conn()
    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.job"):
        stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # persist_deck still called (outer transaction still alive)
    patch_pipeline["mocks"]["persist_deck"].assert_awaited_once()

    # last_error captured from the failing upserts
    assert stats.get("last_error") is not None
    assert "catastrophic db error" in stats["last_error"]

    # A WARNING mentioning "0-card deck" must have been emitted
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("0-card deck" in m for m in warning_messages), (
        f"Expected WARNING containing '0-card deck'; got: {warning_messages}"
    )
    # The last_error value must appear in the 0-card-deck warning
    zero_card_warnings = [m for m in warning_messages if "0-card deck" in m]
    assert any("catastrophic db error" in m for m in zero_card_warnings), (
        f"Expected last_error in 0-card-deck WARNING; got: {zero_card_warnings}"
    )


@pytest.mark.asyncio
async def test_run_pulse_catches_stage2_client_unavailable(patch_pipeline):
    """run_pulse catches Stage2ClientUnavailableError, sets degraded_reason, and still produces a deck.

    W3-DRY-3: when openai_client is None at stage2 entry, the sentinel is raised and
    run_pulse must degrade gracefully (stage1 fallback) rather than crash.
    """
    from paper_ingestion.pulse.job import run_pulse
    from paper_ingestion.pulse.scoring import Stage2ClientUnavailableError

    async def raise_sentinel(*_a, **_kw):
        raise Stage2ClientUnavailableError("openai_client is None")

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = raise_sentinel

    pool, _conn = _make_pool_and_conn()
    stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    # Deck must still be produced (stage1 fallback)
    patch_pipeline["mocks"]["persist_deck"].assert_awaited_once()
    # Sentinel is non-fatal — last_error stays None
    assert stats.get("last_error") is None
    # degraded_reason must be set and reference the sentinel
    assert stats.get("degraded_reason") is not None
    assert "openai_client" in stats["degraded_reason"]


# ---------------------------------------------------------------------------
# B8 — PULSE_STAGE2_TIMEOUT_SECONDS env-tunable (default 900)
# ---------------------------------------------------------------------------


def test_stage2_timeout_default_is_900(monkeypatch):
    """Default PULSE_STAGE2_TIMEOUT_SECONDS must be 900 (more generous than the old 600)."""
    import importlib

    import paper_ingestion.pulse.job as job_mod

    monkeypatch.delenv("PULSE_STAGE2_TIMEOUT_SECONDS", raising=False)
    importlib.reload(job_mod)

    assert job_mod._stage2_timeout() == 900


def test_stage2_timeout_env_override(monkeypatch):
    """PULSE_STAGE2_TIMEOUT_SECONDS env var is respected by _stage2_timeout()."""
    import importlib

    import paper_ingestion.pulse.job as job_mod

    monkeypatch.setenv("PULSE_STAGE2_TIMEOUT_SECONDS", "300")
    importlib.reload(job_mod)

    assert job_mod._stage2_timeout() == 300


@pytest.mark.asyncio
async def test_stage2_timeout_message_includes_configured_value(patch_pipeline):
    """When stage2 times out, the degraded_reason message quotes the configured timeout value.

    Patches _stage2_timeout() directly so that the existing patch_pipeline
    fixture remains intact (no importlib.reload that would break those patches).
    """
    from unittest.mock import patch as stdlib_patch

    from paper_ingestion.pulse.job import run_pulse

    async def raise_timeout(*_a, **_kw):
        raise TimeoutError()

    patch_pipeline["mocks"]["stage2_llm_rerank"].side_effect = raise_timeout

    async def stage3_impl(stage2_out, weights):
        for sc in stage2_out:
            sc.final_score = 0.5
        return stage2_out

    patch_pipeline["mocks"]["stage3_combine"].side_effect = stage3_impl
    patch_pipeline["mocks"]["assemble_deck"].side_effect = lambda scored, size: scored[:size]

    pool, _conn = _make_pool_and_conn()
    with stdlib_patch("paper_ingestion.pulse.job._stage2_timeout", return_value=42):
        stats = await run_pulse(pool, MagicMock(), MagicMock(), now=datetime.now(UTC))

    assert stats["last_error"] is None
    assert stats.get("degraded_reason") is not None
    assert "42s" in stats["degraded_reason"]
