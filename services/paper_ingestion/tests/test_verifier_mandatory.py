"""WS-2 Phase 1: QuoteVerifier mandatory in Pulse scoring + Weekly digest.

Tests required by the WS-2 backend spec:

1. TypeError when verifier omitted from stage2_llm_rerank.
2. TypeError when verifier omitted from generate_weekly_summary.
3. Pulse run end-to-end with stubbed verifier produces cards with verified
   field populated (True for matching reasoning, False for mismatched).
4. Weekly digest produces themes with verified/unverified split populated.
5. After Pulse run, verification_stats are attached to the stats dict and
   a system_events row with event_type='pulse.verification_stats' exists
   (checked via log_event mock).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.extraction.verify import QuoteVerifier
from paper_ingestion.models import PaperCreate, SourceType, TopicRef
from paper_ingestion.pulse.models import PulseScoringOutput
from paper_ingestion.pulse.profile import UserProfile
from paper_ingestion.pulse.scoring import ScoredCandidate, stage2_llm_rerank
from paper_ingestion.weekly_summary import generate_weekly_summary
from paper_ingestion.weekly_summary_models import ThemeOutput, WeeklyDigestOutput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paper(idx: int = 0, abstract: str = "Abstract text.") -> PaperCreate:
    return PaperCreate(
        external_id=f"arxiv:{idx:04d}",
        source_type=SourceType.ARXIV,
        title=f"Test Paper {idx}",
        authors=["Author A"],
        abstract=abstract,
        published_date=datetime(2026, 1, 1).date(),
        url=f"https://arxiv.org/abs/{idx:04d}",
    )


def _make_scored(paper: PaperCreate) -> ScoredCandidate:
    return ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.5, "topic": 0.4, "recency": 0.9, "author_bonus": 0.0},
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )


def _make_profile() -> UserProfile:
    return UserProfile(
        topics=[TopicRef(id=1, name="ML", description="Machine learning")],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={"embedding": 0.3, "topic": 0.3, "llm_relevance": 0.2, "llm_novelty": 0.2},
        deck_size=5,
        stage2_top_k=10,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )


def _make_weekly_pool(rows: list) -> MagicMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


def _make_paper_row(paper_id: int, topic_name: str, summary_brief: str = "") -> dict:
    return {
        "id": paper_id,
        "title": f"Paper {paper_id}",
        "url": f"https://arxiv.org/abs/{paper_id}",
        "published_date": datetime(2026, 3, 1, tzinfo=UTC),
        "authors": ["Author"],
        "topic_name": topic_name,
        "topic_id": 1,
        "relevance_score": 0.9,
        "summary_brief": summary_brief,
        "confidence": "HIGH",
    }


# ---------------------------------------------------------------------------
# 1. TypeError when verifier omitted — stage2_llm_rerank
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_missing_verifier_raises_type_error():
    """stage2_llm_rerank raises TypeError when verifier is not supplied.

    WS-2 Phase 1: verifier is now a required positional parameter with no
    default.  Python raises TypeError at call time before any async work.
    """
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    with pytest.raises(TypeError):
        # verifier omitted — TypeError expected from Python's own argument check
        await stage2_llm_rerank(  # type: ignore[call-arg]
            stage1_out, profile, MagicMock(), openai_client=MagicMock()
        )


# ---------------------------------------------------------------------------
# 2. TypeError when verifier omitted — generate_weekly_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weekly_summary_missing_verifier_type_error():
    """generate_weekly_summary raises TypeError when verifier is not supplied."""
    pool = _make_weekly_pool([])

    with pytest.raises(TypeError):
        await generate_weekly_summary(  # type: ignore[call-arg]
            pool, AsyncMock(), days=7, openai_client=MagicMock()
        )


# ---------------------------------------------------------------------------
# 3. Pulse stage2 — verified field populated (True and False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_verified_field_populated_true_and_false():
    """Cards have reasoning_verified populated by a real QuoteVerifier.

    Paper 0 has abstract containing the reasoning → verified=True.
    Paper 1 has unrelated abstract → verified=False.
    """
    verifier = QuoteVerifier()

    abstract_0 = (
        "We propose Neural ODEs, a family of models that parameterize the "
        "derivative of the hidden state using a neural network."
    )
    abstract_1 = "A study of classical sorting algorithms on random permutations."

    paper0 = _make_paper(0, abstract=abstract_0)
    paper1 = _make_paper(1, abstract=abstract_1)

    # Reasoning for paper0 directly quotes the abstract → should verify True.
    reasoning_0 = "parameterize the derivative of the hidden state using a neural network"
    # Reasoning for paper1 is unrelated → should verify False.
    reasoning_1 = "quantum annealing surpasses gradient descent on QUBO instances"

    def _make_output(reasoning: str) -> PulseScoringOutput:
        return PulseScoringOutput(relevance=7, novelty=5, reasoning=reasoning)

    call_count = [0]
    reasonings = [reasoning_0, reasoning_1]

    async def mock_llm(*args, **kwargs):
        r = reasonings[call_count[0]]
        call_count[0] += 1
        return _make_output(r)

    stage1_out = [_make_scored(paper0), _make_scored(paper1)]
    profile = _make_profile()

    with patch("paper_ingestion.pulse.scoring.call_llm_structured", side_effect=mock_llm):
        result = await stage2_llm_rerank(
            stage1_out,
            profile,
            MagicMock(),
            verifier=verifier,
            openai_client=MagicMock(),
        )

    assert len(result) == 2

    # Both cards must have reasoning_verified populated (not None).
    for sc in result:
        assert sc.reasoning_verified is not None, (
            f"reasoning_verified is None for {sc.paper.external_id}"
        )

    verified_card = next(sc for sc in result if sc.paper.external_id == "arxiv:0000")
    unverified_card = next(sc for sc in result if sc.paper.external_id == "arxiv:0001")

    assert verified_card.reasoning_verified is True
    assert unverified_card.reasoning_verified is False


# ---------------------------------------------------------------------------
# 4. Weekly digest — verified/unverified theme split with mandatory verifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weekly_digest_theme_split_with_mandatory_verifier():
    """Weekly digest produces verified/unverified split when verifier is supplied.

    Theme quoting the corpus lands in verified_themes; unrelated theme
    lands in unverified_themes.
    """
    verifier = QuoteVerifier()

    rows = [
        _make_paper_row(
            1,
            "NLP",
            "Transformers use multi-head self-attention for sequence modeling.",
        ),
        _make_paper_row(
            2,
            "NLP",
            "BERT uses masked language modeling on large text corpora.",
        ),
    ]
    pool = _make_weekly_pool(rows)

    theme_objects = [
        ThemeOutput(
            theme="Transformers use multi-head self-attention for sequence modeling",
            supporting_papers=[1],
            notes=None,
        ),
        ThemeOutput(
            theme="Quantum annealing surpasses classical optimization on QUBO instances",
            supporting_papers=[2],
            notes=None,
        ),
    ]
    llm_output = WeeklyDigestOutput(themes=theme_objects, summary="NLP research summary this week.")

    with patch(
        "paper_ingestion.weekly_summary.call_llm_structured",
        AsyncMock(return_value=llm_output),
    ):
        result = await generate_weekly_summary(
            pool,
            AsyncMock(),
            days=7,
            verifier=verifier,
            openai_client=MagicMock(),
        )

    nlp = next(t for t in result["topics"] if t["name"] == "NLP")
    assert "verified_themes" in nlp
    assert "unverified_themes" in nlp

    # Theme 1 quotes the corpus → verified.
    assert any("multi-head self-attention" in t["theme"] for t in nlp["verified_themes"])
    # Theme 2 is unrelated → unverified.
    assert any("Quantum annealing" in t["theme"] for t in nlp["unverified_themes"])
    # Integrity: all themes accounted for.
    assert len(nlp["verified_themes"]) + len(nlp["unverified_themes"]) == len(nlp["themes"])


# ---------------------------------------------------------------------------
# 5. Pulse verification_stats in stats dict + log_event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pulse_emits_verification_stats_after_run():
    """run_pulse populates stats['verification_stats'] and calls log_event.

    Verifies shape: pass_rate, total, passed, failed — all present.
    Uses a mocked log_event to avoid DB dependency.
    """
    from paper_ingestion.pulse.job import run_pulse
    from paper_ingestion.pulse.scoring import ScoredCandidate
    from tests.conftest import _make_pool_and_conn

    paper = _make_paper(0, abstract="Neural ODEs for dynamics modeling.")
    verified_card = ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.8, "topic": 0.6, "recency": 0.9, "author_bonus": 0.0},
        llm_relevance=8,
        llm_novelty=6,
        reasoning="relevant",
        final_score=0.8,
        reasoning_verified=True,
    )
    unverified_card = ScoredCandidate(
        paper=_make_paper(1),
        signals={"embedding": 0.5, "topic": 0.4, "recency": 0.8, "author_bonus": 0.0},
        llm_relevance=5,
        llm_novelty=4,
        reasoning="unrelated claim",
        final_score=0.5,
        reasoning_verified=False,
    )
    stage2_out = [verified_card, unverified_card]

    profile = _make_profile()
    pool, _conn = _make_pool_and_conn()

    log_event_calls: list[dict] = []

    async def _capture_log_event(**kwargs):
        log_event_calls.append(kwargs)

    with (
        patch("paper_ingestion.pulse.job.load_profile", AsyncMock(return_value=profile)),
        patch(
            "paper_ingestion.pulse.job.discover_candidates",
            AsyncMock(return_value=([paper], {"arxiv": 1}, {})),
        ),
        patch(
            "paper_ingestion.pulse.job.stage1_embedding_filter",
            AsyncMock(return_value=stage2_out),
        ),
        patch(
            "paper_ingestion.pulse.job.stage2_llm_rerank",
            AsyncMock(return_value=stage2_out),
        ),
        patch("paper_ingestion.pulse.job.stage3_combine", AsyncMock(return_value=stage2_out)),
        patch("paper_ingestion.pulse.job.assemble_deck", MagicMock(return_value=stage2_out)),
        patch("paper_ingestion.pulse.job.upsert_paper", AsyncMock(return_value={"id": 1})),
        patch("paper_ingestion.pulse.job.persist_deck", AsyncMock(return_value=2)),
        patch("paper_ingestion.pulse.job.log_event", _capture_log_event),
    ):
        stats = await run_pulse(pool, MagicMock(), MagicMock())

    # stats dict must contain verification_stats.
    assert "verification_stats" in stats, "stats missing verification_stats key"
    vs = stats["verification_stats"]
    assert "pass_rate" in vs
    assert "total" in vs
    assert "passed" in vs
    assert "failed" in vs

    # With 1 verified and 1 unverified card:
    assert vs["total"] == 2
    assert vs["passed"] == 1
    assert vs["failed"] == 1
    assert abs(vs["pass_rate"] - 0.5) < 1e-6

    # log_event must have been called with the right shape.
    pulse_stat_events = [
        e for e in log_event_calls if e.get("message") == "pulse.verification_stats"
    ]
    assert len(pulse_stat_events) >= 1, (
        f"No pulse.verification_stats log_event emitted; all calls: {log_event_calls}"
    )
    evt = pulse_stat_events[0]
    assert evt["category"] == "job"
    assert evt["source"] == "pulse"
    ctx = evt["context"]
    assert "pass_rate" in ctx
    assert "total" in ctx
    assert "passed" in ctx
    assert "failed" in ctx
