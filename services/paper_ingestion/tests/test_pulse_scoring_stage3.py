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


def _make_scored(
    paper: PaperCreate,
    signals: dict,
    llm_relevance: int | None = None,
    llm_novelty: int | None = None,
) -> ScoredCandidate:
    return ScoredCandidate(
        paper=paper,
        signals=signals,
        llm_relevance=llm_relevance,
        llm_novelty=llm_novelty,
        reasoning=None,
        final_score=None,
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
    sc = _make_scored(paper, signals)

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
    sc = _make_scored(paper, signals)

    result = await stage3_combine([sc], weights)

    assert abs(result[0].final_score - 1.0) < 1e-9


@pytest.mark.asyncio
async def test_stage3_sort_order_descending():
    """Output is sorted by final_score descending."""
    papers = [_make_paper(i) for i in range(4)]
    # Assign different embedding scores
    scores = [0.1, 0.9, 0.5, 0.3]
    candidates = [_make_scored(p, {"embedding": s}) for p, s in zip(papers, scores)]
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
    sc = _make_scored(paper, {"embedding": 0.0, "topic": 0.0, "recency": 0.0})
    result = await stage3_combine([sc], _WEIGHTS)
    assert result[0].final_score == 0.0


@pytest.mark.asyncio
async def test_stage3_weight_not_in_signals():
    """Weight keys not in signals dict are treated as signal=0."""
    paper = _make_paper(0)
    sc = _make_scored(paper, {"embedding": 0.5})
    weights = {"embedding": 0.5, "topic": 0.5}  # topic missing from signals
    result = await stage3_combine([sc], weights)
    # 0.5 * 0.5 + 0.0 * 0.5 = 0.25
    assert abs(result[0].final_score - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_stage3_preserves_all_candidate_data():
    """stage3_combine preserves paper, llm_relevance, llm_novelty, reasoning."""
    paper = _make_paper(0)
    sc = _make_scored(paper, {"embedding": 0.8}, llm_relevance=8, llm_novelty=6)
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
    candidates = [_make_scored(p, {"embedding": 0.5 + i * 0.1}) for i, p in enumerate(papers)]

    result = await stage3_combine(candidates, _WEIGHTS)

    assert all(sc.final_score is not None for sc in result)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_stage3_empty_weights_gives_zero():
    """Empty weights dict gives 0.0 for all candidates."""
    papers = [_make_paper(i) for i in range(3)]
    candidates = [_make_scored(p, {"embedding": 0.9}) for p in papers]

    result = await stage3_combine(candidates, {})

    for sc in result:
        assert sc.final_score == 0.0
