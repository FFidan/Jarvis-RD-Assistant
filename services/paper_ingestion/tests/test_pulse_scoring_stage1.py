"""Tests for app.pulse.scoring — stage1_embedding_filter.

TDD: tests written before implementation.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from paper_ingestion.models import PaperCreate, SourceType, TopicRef
from paper_ingestion.pulse.profile import UserProfile
from paper_ingestion.pulse.scoring import stage1_embedding_filter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paper(
    title: str = "Test Paper",
    abstract: str = "Test abstract",
    published_date: date | None = None,
    authors: list[str] | None = None,
    external_id: str = "arxiv:0001",
) -> PaperCreate:
    return PaperCreate(
        external_id=external_id,
        source_type=SourceType.ARXIV,
        title=title,
        authors=authors or ["Author A"],
        abstract=abstract,
        published_date=published_date or date.today(),
        url=f"https://arxiv.org/abs/{external_id}",
    )


def _make_profile(
    centroid: list[float] | None = None,
    topics: list[TopicRef] | None = None,
    tracked_author_names: set[str] | None = None,
    tracked_author_s2_ids: set[str] | None = None,
    negative_centroid: list[float] | None = None,
    l2_lambda: float = 0.5,
) -> UserProfile:
    return UserProfile(
        topics=topics or [],
        tracked_author_names=tracked_author_names or set(),
        tracked_author_s2_ids=tracked_author_s2_ids or set(),
        library_centroid=centroid,
        negative_centroid=negative_centroid,
        weights={
            "embedding": 0.2,
            "topic": 0.2,
            "llm_relevance": 0.3,
            "llm_novelty": 0.1,
            "author_bonus": 0.15,
            "recency": 0.05,
            "l2_lambda": l2_lambda,
        },
        deck_size=10,
        stage2_top_k=50,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )


def _make_embedder(return_vecs: list[list[float]]) -> AsyncMock:
    mock = AsyncMock()
    mock.embed_texts.return_value = return_vecs
    return mock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_returns_sorted_top_k():
    """With 5 candidates and top_k=2, returns 2 ScoredCandidates sorted desc."""
    # Create 5 papers with same-day publication
    papers = [_make_paper(title=f"Paper {i}", external_id=f"arxiv:{i}") for i in range(5)]
    # Create embeddings: first is most similar to centroid (same direction), rest orthogonal
    vecs = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
        [-1.0, 0.0, 0.0, 0.0],
    ]
    centroid_vec = [1.0, 0.0, 0.0, 0.0]  # same as vecs[0]
    profile = _make_profile(centroid=centroid_vec)
    # embed_texts is called once for topics (empty) + once for candidates
    # With no topics, embed_texts is called once for the 5 candidates
    embedder = _make_embedder(vecs)

    result = await stage1_embedding_filter(papers, profile, embedder, top_k=2)

    assert len(result) == 2
    # Best score first (vecs[0] most similar to centroid_vec)
    assert result[0].signals["embedding"] > result[1].signals["embedding"]


@pytest.mark.asyncio
async def test_stage1_empty_candidates_returns_empty():
    """Empty input returns empty list without calling embedder."""
    profile = _make_profile()
    embedder = _make_embedder([])

    result = await stage1_embedding_filter([], profile, embedder, top_k=10)

    assert result == []
    embedder.embed_texts.assert_not_called()


@pytest.mark.asyncio
async def test_stage1_centroid_none_embedding_sim_zero():
    """When library_centroid is None, embedding_sim signal is 0.0 for all candidates."""
    papers = [_make_paper(title="P1", external_id="arxiv:1")]
    profile = _make_profile(centroid=None)
    embedder = _make_embedder([[0.5, 0.5, 0.5, 0.5]])

    result = await stage1_embedding_filter(papers, profile, embedder, top_k=10)

    assert len(result) == 1
    assert result[0].signals["embedding"] == 0.0


@pytest.mark.asyncio
async def test_stage1_recency_decay_newer_higher():
    """A paper published today scores higher recency than one published 60 days ago."""
    today = date.today()
    old_date = today - timedelta(days=60)

    new_paper = _make_paper(title="New", published_date=today, external_id="arxiv:new")
    old_paper = _make_paper(title="Old", published_date=old_date, external_id="arxiv:old")

    # Use identical embeddings so only recency differs
    vec = [1.0, 0.0, 0.0, 0.0]
    centroid = [1.0, 0.0, 0.0, 0.0]
    profile = _make_profile(centroid=centroid)
    embedder = _make_embedder([vec, vec])

    result = await stage1_embedding_filter([new_paper, old_paper], profile, embedder, top_k=10)

    new_sc = next(c for c in result if c.paper.title == "New")
    old_sc = next(c for c in result if c.paper.title == "Old")
    assert new_sc.signals["recency"] > old_sc.signals["recency"]


@pytest.mark.asyncio
async def test_stage1_recency_decay_formula():
    """recency_decay = exp(-age_days / 30), today = 1.0."""
    today = date.today()
    paper = _make_paper(published_date=today, external_id="arxiv:today")
    profile = _make_profile(centroid=None)
    embedder = _make_embedder([[0.5, 0.5]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    # today → age_days=0 → exp(0) = 1.0
    assert abs(result[0].signals["recency"] - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_stage1_recency_none_date_handled():
    """Paper with published_date=None does not crash; recency treated as 0.0 or clamped."""
    paper = _make_paper(published_date=None, external_id="arxiv:nodate")
    paper = PaperCreate(
        external_id="arxiv:nodate",
        source_type=SourceType.ARXIV,
        title="No Date Paper",
        authors=["A"],
        abstract="abs",
        published_date=None,
        url="https://arxiv.org/abs/nodate",
    )
    profile = _make_profile(centroid=None)
    embedder = _make_embedder([[0.5, 0.5]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    assert len(result) == 1
    # Should not crash; recency should be 0.0 when date is None
    assert 0.0 <= result[0].signals["recency"] <= 1.0


@pytest.mark.asyncio
async def test_stage1_author_bonus_applied():
    """Author bonus is 1.0 when candidate author name matches a tracked author."""
    # Author bonus uses dual-set matching: display names (lowercased) OR s2 IDs.
    # PaperCreate.authors is list[str] (display names); they are lowercased for comparison.
    # This test verifies the bonus is applied when an author name matches.
    papers = [
        _make_paper(title="Tracked Author Paper", authors=["Jane Doe"], external_id="arxiv:t1"),
        _make_paper(
            title="Unknown Author Paper", authors=["Random Person"], external_id="arxiv:t2"
        ),
    ]
    vec = [1.0, 0.0]
    profile = _make_profile(centroid=None, tracked_author_names={"jane doe"})
    embedder = _make_embedder([vec, vec])

    result = await stage1_embedding_filter(papers, profile, embedder, top_k=10)

    tracked_sc = next(c for c in result if c.paper.title == "Tracked Author Paper")
    unknown_sc = next(c for c in result if c.paper.title == "Unknown Author Paper")
    assert tracked_sc.signals["author_bonus"] == 1.0
    assert unknown_sc.signals["author_bonus"] == 0.0


@pytest.mark.asyncio
async def test_stage1_with_topics_max_topic_sim():
    """topic_sim is max cosine similarity over all topic embeddings."""
    # Two topics with known embeddings
    topics = [
        TopicRef(id=1, name="Topic A", description=None),
        TopicRef(id=2, name="Topic B", description=None),
    ]
    # Candidate embedding aligns with topic B
    candidate_vec = [0.0, 1.0, 0.0, 0.0]
    topic_a_vec = [1.0, 0.0, 0.0, 0.0]
    topic_b_vec = [0.0, 1.0, 0.0, 0.0]

    profile = _make_profile(centroid=None, topics=topics)
    papers = [_make_paper(title="Test", external_id="arxiv:ts")]

    # embed_texts called: first for topics (2 calls), then for candidates (1 call)
    embedder = AsyncMock()
    # First call: topic embeddings; Second call: candidate embeddings
    embedder.embed_texts.side_effect = [
        [topic_a_vec, topic_b_vec],  # topics
        [candidate_vec],  # candidates
    ]

    result = await stage1_embedding_filter(papers, profile, embedder, top_k=10)

    # cosine([0,1,0,0], [0,1,0,0]) = 1.0
    assert abs(result[0].signals["topic"] - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_stage1_top_k_limits_output():
    """top_k limits output even when more candidates are available."""
    papers = [_make_paper(title=f"P{i}", external_id=f"arxiv:{i}") for i in range(20)]
    vecs = [[float(i), 0.0] for i in range(20)]
    profile = _make_profile(centroid=None)
    embedder = _make_embedder(vecs)

    result = await stage1_embedding_filter(papers, profile, embedder, top_k=5)

    assert len(result) == 5


@pytest.mark.asyncio
async def test_stage1_scored_candidate_has_expected_signals():
    """ScoredCandidate carries all expected signal keys."""
    paper = _make_paper()
    profile = _make_profile(centroid=None)
    embedder = _make_embedder([[0.5, 0.5]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    signals = result[0].signals
    for key in ("embedding", "topic", "recency", "author_bonus", "l2_penalty"):
        assert key in signals, f"Missing signal: {key}"


# ---------------------------------------------------------------------------
# L2 negative-centroid penalty (Wave 1cd §7.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_no_negative_centroid_no_penalty():
    """When profile.negative_centroid is None, l2_penalty signal is 0.0."""
    paper = _make_paper()
    profile = _make_profile(centroid=None, negative_centroid=None)
    embedder = _make_embedder([[1.0, 0.0, 0.0, 0.0]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    assert len(result) == 1
    assert result[0].signals["l2_penalty"] == 0.0


@pytest.mark.asyncio
async def test_stage1_with_negative_centroid_applies_penalty():
    """When negative_centroid is set, l2_penalty equals lambda * cosine(cand, neg_centroid).

    Uses unit vectors so cosine similarity = 1.0 for perfectly aligned candidate.
    Expected penalty = l2_lambda * 1.0.
    The embedding signal is reduced by this penalty (embedding_sim - penalty).
    """
    # Candidate and negative centroid point in the same direction (cosine=1.0)
    cand_vec = [1.0, 0.0, 0.0, 0.0]
    neg_centroid = [1.0, 0.0, 0.0, 0.0]
    l2_lambda = 0.5

    paper = _make_paper()
    profile = _make_profile(
        centroid=None,  # no positive centroid — isolates L2 path
        negative_centroid=neg_centroid,
        l2_lambda=l2_lambda,
    )
    embedder = _make_embedder([cand_vec])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    assert len(result) == 1
    signals = result[0].signals
    expected_penalty = l2_lambda * 1.0  # cosine([1,0,0,0], [1,0,0,0]) = 1.0
    assert abs(signals["l2_penalty"] - expected_penalty) < 1e-6
    # embedding signal = 0.0 (no positive centroid) - penalty → negative
    assert abs(signals["embedding"] - (0.0 - expected_penalty)) < 1e-6


@pytest.mark.asyncio
async def test_stage1_with_negative_centroid_orthogonal_no_penalty():
    """Candidate orthogonal to negative_centroid gets zero penalty (cosine=0.0)."""
    cand_vec = [0.0, 1.0, 0.0, 0.0]  # orthogonal to neg_centroid
    neg_centroid = [1.0, 0.0, 0.0, 0.0]
    l2_lambda = 0.5

    paper = _make_paper()
    profile = _make_profile(
        centroid=None,
        negative_centroid=neg_centroid,
        l2_lambda=l2_lambda,
    )
    embedder = _make_embedder([cand_vec])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    assert len(result) == 1
    # cosine([0,1,0,0], [1,0,0,0]) = 0.0 → penalty = 0.0
    assert abs(result[0].signals["l2_penalty"]) < 1e-6
