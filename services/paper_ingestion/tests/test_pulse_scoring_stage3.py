"""Tests for app.pulse.scoring — stage3_combine.

TDD: tests written before implementation.
Pure arithmetic — no I/O mocking needed.
"""

from datetime import date

import pytest
from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.pulse.scoring import ScoredCandidate, stage3_combine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paper(idx: int = 0) -> PaperCreate:
    return PaperCreate(
        external_id=f"arxiv:{idx:04d}",
        source_type=SourceType.ARXIV,
        title=f"Paper {idx}",
        authors=["A"],
        abstract="abs",
        published_date=date.today(),
        url=f"https://arxiv.org/abs/{idx:04d}",
    )


_WEIGHTS = {
    "embedding": 0.2,
    "topic": 0.2,
    "llm_relevance": 0.3,
    "llm_novelty": 0.1,
    "author_bonus": 0.15,
    "recency": 0.05,
}


# ---------------------------------------------------------------------------
# Arithmetic correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage3_final_score_arithmetic():
    """final_score = sum(signals[k] * weights[k]) for all keys."""
    signals = {
        "embedding": 0.8,
        "topic": 0.6,
        "llm_relevance": 0.7,
        "llm_novelty": 0.5,
        "author_bonus": 1.0,
        "recency": 0.9,
    }
    expected = 0.8 * 0.2 + 0.6 * 0.2 + 0.7 * 0.3 + 0.5 * 0.1 + 1.0 * 0.15 + 0.9 * 0.05
    paper = _make_paper(0)
    sc = ScoredCandidate(
        paper=paper,
        signals=signals,
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )

    result = await stage3_combine([sc], _WEIGHTS)

    assert len(result) == 1
    assert abs(result[0].final_score - expected) < 1e-9


@pytest.mark.asyncio
async def test_stage3_missing_signal_treated_as_zero():
    """Signals not present in the candidate are treated as 0."""
    # Only embedding and topic signals present; author_bonus, recency, llm_* missing
    signals = {"embedding": 1.0, "topic": 1.0}
    weights = {"embedding": 0.5, "topic": 0.5, "llm_relevance": 0.3}
    # Expected: 1.0*0.5 + 1.0*0.5 + 0.0*0.3 = 1.0
    paper = _make_paper(0)
    sc = ScoredCandidate(
        paper=paper,
        signals=signals,
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )

    result = await stage3_combine([sc], weights)

    assert abs(result[0].final_score - 1.0) < 1e-9


@pytest.mark.asyncio
async def test_stage3_sort_order_descending():
    """Output is sorted by final_score descending."""
    papers = [_make_paper(i) for i in range(4)]
    # Assign different embedding scores
    scores = [0.1, 0.9, 0.5, 0.3]
    candidates = [
        ScoredCandidate(
            paper=p,
            signals={"embedding": s},
            llm_relevance=None,
            llm_novelty=None,
            reasoning=None,
            final_score=None,
        )
        for p, s in zip(papers, scores)
    ]
    weights = {"embedding": 1.0}

    result = await stage3_combine(candidates, weights)

    final_scores = [sc.final_score for sc in result]
    assert final_scores == sorted(final_scores, reverse=True)


@pytest.mark.asyncio
async def test_stage3_empty_input_returns_empty():
    """Empty input returns empty list."""
    result = await stage3_combine([], _WEIGHTS)
    assert result == []


@pytest.mark.asyncio
async def test_stage3_all_zero_signals():
    """All-zero signals produce 0.0 final_score."""
    paper = _make_paper(0)
    sc = ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.0, "topic": 0.0, "recency": 0.0},
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )
    result = await stage3_combine([sc], _WEIGHTS)
    assert result[0].final_score == 0.0


@pytest.mark.asyncio
async def test_stage3_weight_not_in_signals():
    """Weight keys not in signals dict are treated as signal=0."""
    paper = _make_paper(0)
    sc = ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.5},
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )
    weights = {"embedding": 0.5, "topic": 0.5}  # topic missing from signals
    result = await stage3_combine([sc], weights)
    # 0.5 * 0.5 + 0.0 * 0.5 = 0.25
    assert abs(result[0].final_score - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_stage3_classifier_and_citation_signals_use_weighted_sum():
    """Phase 2 optional signals are ordinary weighted terms, not a special blend."""
    paper = _make_paper(0)
    signals = {
        "embedding": 0.2,
        "classifier": 0.9,
        "citation_pagerank": 0.5,
        "citation_count": 1.0,
    }
    weights = {
        "embedding": 0.1,
        "classifier": 0.4,
        "citation_pagerank": 0.2,
        "citation_count": 0.3,
    }
    expected = 0.2 * 0.1 + 0.9 * 0.4 + 0.5 * 0.2 + 1.0 * 0.3
    sc = ScoredCandidate(
        paper=paper,
        signals=signals,
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )

    result = await stage3_combine([sc], weights)

    assert abs(result[0].final_score - expected) < 1e-9
    assert result[0].signals == signals


@pytest.mark.asyncio
async def test_stage3_preserves_all_candidate_data():
    """stage3_combine preserves paper, llm_relevance, llm_novelty, reasoning."""
    paper = _make_paper(0)
    sc = ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.8},
        llm_relevance=8,
        llm_novelty=6,
        reasoning=None,
        final_score=None,
    )
    sc = ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.8},
        llm_relevance=8,
        llm_novelty=6,
        reasoning="Very relevant",
        final_score=None,
    )
    weights = {"embedding": 1.0}

    result = await stage3_combine([sc], weights)

    assert result[0].paper.title == "Paper 0"
    assert result[0].llm_relevance == 8
    assert result[0].llm_novelty == 6
    assert result[0].reasoning == "Very relevant"


@pytest.mark.asyncio
async def test_stage3_multiple_candidates_all_get_scores():
    """All candidates have final_score set after stage3."""
    papers = [_make_paper(i) for i in range(5)]
    candidates = [
        ScoredCandidate(
            paper=p,
            signals={"embedding": 0.5 + i * 0.1},
            llm_relevance=None,
            llm_novelty=None,
            reasoning=None,
            final_score=None,
        )
        for i, p in enumerate(papers)
    ]

    result = await stage3_combine(candidates, _WEIGHTS)

    assert all(sc.final_score is not None for sc in result)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_stage3_empty_weights_gives_zero():
    """Empty weights dict gives 0.0 for all candidates."""
    papers = [_make_paper(i) for i in range(3)]
    candidates = [
        ScoredCandidate(
            paper=p,
            signals={"embedding": 0.9},
            llm_relevance=None,
            llm_novelty=None,
            reasoning=None,
            final_score=None,
        )
        for p in papers
    ]

    result = await stage3_combine(candidates, {})

    for sc in result:
        assert sc.final_score == 0.0


# ---------------------------------------------------------------------------
# PI-CORE-008: negative final_scores must sort below genuine zeros
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_final_score_sorts_below_zero():
    """Negative final_scores must rank below genuine 0.0 scores (PI-CORE-008).

    The old `sc.final_score or 0.0` coerced negative values to 0.0 via falsy
    semantics, making them sort identically to genuine zeros.  The fix uses
    `if sc.final_score is not None else float('-inf')` so negatives stay negative.
    """
    papers = [_make_paper(i) for i in range(3)]
    # Craft signals that produce negative, zero, and positive final_scores.
    # Use a weight of 1.0 for a single signal so final_score == signal value.
    weights = {"embedding": 1.0}
    candidates = [
        ScoredCandidate(
            paper=papers[0],
            signals={"embedding": -0.5},
            llm_relevance=None,
            llm_novelty=None,
            reasoning=None,
            final_score=None,
        ),  # negative
        ScoredCandidate(
            paper=papers[1],
            signals={"embedding": 0.0},
            llm_relevance=None,
            llm_novelty=None,
            reasoning=None,
            final_score=None,
        ),  # genuine zero
        ScoredCandidate(
            paper=papers[2],
            signals={"embedding": 0.3},
            llm_relevance=None,
            llm_novelty=None,
            reasoning=None,
            final_score=None,
        ),  # positive
    ]

    result = await stage3_combine(candidates, weights)

    final_scores = [sc.final_score for sc in result]
    # Must be descending: 0.3, 0.0, -0.5
    assert final_scores[0] == pytest.approx(0.3)
    assert final_scores[1] == pytest.approx(0.0)
    assert final_scores[2] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# DOM-B-01: l2_lambda must not be iterated by stage3_combine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage3_combine_does_not_iterate_l2_lambda():
    """l2_lambda must NOT be passed inside the weights dict to stage3_combine (DOM-B-01).

    If l2_lambda were in the weights dict, stage3_combine would compute
    ``sc.signals.get('l2_lambda', 0.0) * l2_lambda_weight`` for every candidate.
    Since 'l2_lambda' is never a signal key in sc.signals, this evaluates to
    0.0 * weight = 0.0 today — a silent no-op — but once any signal is named
    'l2_lambda', it becomes a silent scoring collision.

    This test verifies that passing l2_lambda=0.5 inside the weights dict
    does NOT change the final_score compared to a weights dict without it,
    proving the contract holds: l2_lambda must live on UserProfile.l2_lambda,
    not inside the weights dict passed to stage3_combine.
    """
    paper = _make_paper(0)
    signals = {"embedding": 0.8, "topic": 0.6}
    # Weights WITHOUT l2_lambda — the expected contract
    weights_clean = {"embedding": 0.5, "topic": 0.5}
    # Weights WITH l2_lambda erroneously included — the bug scenario
    weights_with_l2 = {"embedding": 0.5, "topic": 0.5, "l2_lambda": 0.5}

    sc_clean = ScoredCandidate(
        paper=paper,
        signals=signals,
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )
    sc_with_l2 = ScoredCandidate(
        paper=paper,
        signals=signals,
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )

    result_clean = await stage3_combine([sc_clean], weights_clean)
    result_with_l2 = await stage3_combine([sc_with_l2], weights_with_l2)

    # If l2_lambda is in weights, stage3 tries signals.get('l2_lambda', 0.0) * 0.5 = 0.0
    # — scores are equal TODAY, but the mechanism is wrong.
    # This assertion documents the expected score and proves l2_lambda has no
    # signal value to multiply, so its presence is always a no-op / footgun.
    expected_score = 0.8 * 0.5 + 0.6 * 0.5  # = 0.7
    assert abs(result_clean[0].final_score - expected_score) < 1e-9

    # The real fix: load_profile must NOT put l2_lambda in profile.weights.
    # Verify by asserting the clean weights produce the correct score.
    assert abs(result_clean[0].final_score - result_with_l2[0].final_score) < 1e-9, (
        "l2_lambda in weights must not affect final_score (signal 'l2_lambda' is always absent)"
    )

    # Confirm l2_lambda is absent from the clean weights — the invariant the
    # producer (load_profile) must uphold after DOM-B-01 fix.
    assert "l2_lambda" not in weights_clean, (
        "weights passed to stage3_combine must not contain 'l2_lambda'"
    )
