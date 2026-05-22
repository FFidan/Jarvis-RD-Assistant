"""Unit tests for the L2 cosine-penalty branch in stage1_embedding_filter.

Wave 1cd §7.2 — B2 added a negative-centroid penalty:
    embedding_sim -= l2_lambda * cosine(cand_vec, negative_centroid)

These tests verify the math in isolation, independent of DB / HTTP / recency.

Time-decay note: the 30-day half-weight lives in the SQL CASE inside
``pulse/profile.py::load_profile`` Phase 2b, NOT in scoring.py.
C1 (test_pulse_profile.py) is the appropriate home for time-decay tests.
"""

from datetime import date
from unittest.mock import AsyncMock

import pytest
from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.pulse.profile import UserProfile
from paper_ingestion.pulse.scoring import stage1_embedding_filter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_DATE = date(2026, 1, 1)  # fixed "today" for deterministic recency


# Keep local: L2-penalty-specific signature (embedding_vec kwarg) not in pulse_helpers.make_pulse_paper.
def _make_paper(
    embedding_vec: list[float] | None = None,
    external_id: str = "arxiv:0001",
) -> PaperCreate:
    """Minimal PaperCreate.  embedding_vec is used to set the mock return value."""
    return PaperCreate(
        external_id=external_id,
        source_type=SourceType.ARXIV,
        title="Test Paper",
        authors=["Author A"],
        abstract="Test abstract",
        published_date=_FIXED_DATE,
        url=f"https://arxiv.org/abs/{external_id}",
    )


# Keep local: L2-penalty-specific profile (zero-weight dims + l2_lambda override) differs from other pulse profiles.
def _make_profile(
    library_centroid: list[float] | None,
    negative_centroid: list[float] | None,
    l2_lambda: float | None = 0.5,
) -> UserProfile:
    """Minimal UserProfile focused on L2-penalty fields."""
    weights: dict[str, float] = {
        "embedding": 1.0,
        "topic": 0.0,
        "llm_relevance": 0.0,
        "llm_novelty": 0.0,
        "author_bonus": 0.0,
        "recency": 0.0,
    }
    profile = UserProfile(
        topics=[],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=library_centroid,
        negative_centroid=negative_centroid,
        weights=weights,
        deck_size=10,
        stage2_top_k=50,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )
    if l2_lambda is not None:
        profile.l2_lambda = l2_lambda
    return profile


# Keep local: returns a plain AsyncMock (not Embedder) with embed_texts wired — canonical testing_embedder._make_embedder() has no args.
def _make_embedder(cand_vecs: list[list[float]]) -> AsyncMock:
    """Embedder mock that returns cand_vecs for candidates (no topics in these tests)."""
    mock = AsyncMock()
    mock.embed_texts.return_value = cand_vecs
    return mock


# ---------------------------------------------------------------------------
# Test 1: No negative centroid → no penalty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_negative_centroid_no_penalty():
    """When negative_centroid is None, l2_penalty == 0.0 and embedding_sim is unmodified.

    candidate = [1, 0, 0], library_centroid = [1, 0, 0]
    cos([1,0,0], [1,0,0]) = 1.0; no penalty applied.
    Expected: signals["embedding"] == 1.0, signals["l2_penalty"] == 0.0.
    """
    paper = _make_paper(external_id="arxiv:t1")
    profile = _make_profile(
        library_centroid=[1.0, 0.0, 0.0],
        negative_centroid=None,
        l2_lambda=0.5,
    )
    embedder = _make_embedder([[1.0, 0.0, 0.0]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10, now=_FIXED_DATE)

    assert len(result) == 1
    signals = result[0].signals
    assert "l2_penalty" not in signals, "l2_penalty must not appear in signals (M-06)"
    assert abs(signals["embedding"] - 1.0) < 1e-9, (
        f"Expected embedding_sim==1.0 (no penalty), got {signals['embedding']}"
    )


# ---------------------------------------------------------------------------
# Test 2: Negative centroid penalty applied (lambda = 0.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_centroid_penalty_applied():
    """Penalty correctly subtracts from embedding_sim.

    library_centroid = [0, 1, 0], negative_centroid = [1, 0, 0], lambda = 0.5
    candidate = [1, 0, 0]

    embedding_sim  = cos([1,0,0], [0,1,0]) = 0.0
    negative_penalty = 0.5 * cos([1,0,0], [1,0,0]) = 0.5 * 1.0 = 0.5
    final embedding_sim = 0.0 - 0.5 = -0.5
    """
    paper = _make_paper(external_id="arxiv:t2")
    profile = _make_profile(
        library_centroid=[0.0, 1.0, 0.0],
        negative_centroid=[1.0, 0.0, 0.0],
        l2_lambda=0.5,
    )
    embedder = _make_embedder([[1.0, 0.0, 0.0]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10, now=_FIXED_DATE)

    signals = result[0].signals
    assert "l2_penalty" not in signals, "l2_penalty must not appear in signals (M-06)"
    assert abs(signals["embedding"] - (-0.5)) < 1e-9, (
        f"Expected embedding==-0.5 (penalty subtracted), got {signals['embedding']}"
    )


# ---------------------------------------------------------------------------
# Test 3: Lambda scaling is linear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lambda_scaling_is_linear():
    """Score decreases linearly with l2_lambda.

    candidate perfectly aligned with negative_centroid, orthogonal to library_centroid.
    cos(cand, library_centroid) = 0, cos(cand, negative_centroid) = 1.
    embedding_sim = 0 - lambda * 1 = -lambda.

    Tested for lambda ∈ {0.0, 0.5, 1.0, 2.0}.
    """
    lambdas = [0.0, 0.5, 1.0, 2.0]
    cand_vec = [1.0, 0.0, 0.0]

    scores: list[float] = []
    for lam in lambdas:
        paper = _make_paper(external_id=f"arxiv:lam{lam}")
        profile = _make_profile(
            library_centroid=[0.0, 1.0, 0.0],
            negative_centroid=[1.0, 0.0, 0.0],
            l2_lambda=lam,
        )
        embedder = _make_embedder([cand_vec])
        result = await stage1_embedding_filter(
            [paper], profile, embedder, top_k=10, now=_FIXED_DATE
        )
        scores.append(result[0].signals["embedding"])

    # Expected: [0.0, -0.5, -1.0, -2.0]
    expected = [-lam for lam in lambdas]
    for lam, got, exp in zip(lambdas, scores, expected):
        assert abs(got - exp) < 1e-9, f"lambda={lam}: expected {exp}, got {got}"

    # Verify strict decrease: each score is less than the previous
    for i in range(1, len(scores)):
        assert scores[i] < scores[i - 1], (
            f"Score did not decrease linearly at index {i}: {scores[i - 1]} → {scores[i]}"
        )


# ---------------------------------------------------------------------------
# Test 4: Default lambda (0.5) applies when l2_lambda absent from weights
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_lambda_applied_when_missing_from_weights():
    """When l2_lambda is absent from profile.weights, the code defaults to 0.5.

    candidate = negative_centroid = [1, 0, 0]; library_centroid orthogonal [0, 1, 0].
    penalty = 0.5 (default) * cos([1,0,0],[1,0,0]) = 0.5 * 1.0 = 0.5.
    embedding_sim = 0.0 - 0.5 = -0.5.
    """
    paper = _make_paper(external_id="arxiv:t4")
    # l2_lambda=None so it is NOT added to weights dict
    profile = _make_profile(
        library_centroid=[0.0, 1.0, 0.0],
        negative_centroid=[1.0, 0.0, 0.0],
        l2_lambda=None,  # absent → code should use .get("l2_lambda", 0.5) → 0.5
    )
    embedder = _make_embedder([[1.0, 0.0, 0.0]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10, now=_FIXED_DATE)

    signals = result[0].signals
    assert "l2_penalty" not in signals, "l2_penalty must not appear in signals (M-06)"
    # Default lambda is 0.5; penalty = 0.5 * 1.0 = 0.5 → embedding = 0.0 - 0.5 = -0.5
    assert abs(signals["embedding"] - (-0.5)) < 1e-9, (
        f"Expected embedding==-0.5 (default lambda penalty applied), got {signals['embedding']}"
    )


# ---------------------------------------------------------------------------
# Test 5 (M-06): l2_penalty key is absent from signals dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l2_penalty_key_absent_from_signals():
    """M-06: signals dict must NOT contain 'l2_penalty' — key dropped, math stays."""
    paper = _make_paper(external_id="arxiv:t5")
    profile = _make_profile(
        library_centroid=[1.0, 0.0, 0.0],
        negative_centroid=None,
        l2_lambda=0.5,
    )
    embedder = _make_embedder([[0.5, 0.5, 0.0]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10, now=_FIXED_DATE)

    assert "l2_penalty" not in result[0].signals, "l2_penalty key must be absent (M-06)"
