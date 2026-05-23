"""Tests for app.pulse.scoring — stage1_embedding_filter.

TDD: tests written before implementation.
"""

import math
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from paper_ingestion.models import PaperCreate, SourceType, TopicRef
from paper_ingestion.pulse.profile import UserProfile
import paper_ingestion.pulse.scoring as _scoring_mod
from paper_ingestion.pulse.scoring import (
    _llm_concurrency,
    _llm_model,
    stage1_embedding_filter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Keep local: richer kwargs signature (abstract/authors/external_id/published_date) not covered by pulse_helpers.make_pulse_paper.
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


# Keep local: stage1-specific weights/recent-titles structure not shared by other pulse tests.
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


# Local helper — canonical testing_embedder._make_embedder returns a real Embedder;
# this variant takes return_vecs and returns a plain AsyncMock (stage-1 protocol only).
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


@pytest.mark.parametrize(
    ("papers_arg", "profile_kwargs", "embedder_vecs", "expected_len", "signal_checks"),
    [
        pytest.param(
            [],
            {},
            [],
            0,
            {},
            id="empty_candidates",
        ),
        pytest.param(
            "single",
            {"centroid": None},
            [[0.5, 0.5, 0.5, 0.5]],
            1,
            {"embedding": 0.0},
            id="centroid_none_embedding_sim_zero",
        ),
        pytest.param(
            "single",
            {"centroid": None, "negative_centroid": None},
            [[1.0, 0.0, 0.0, 0.0]],
            1,
            {"__absent__": "l2_penalty"},
            id="no_negative_centroid_no_penalty",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stage1_zero_signal_cluster(
    papers_arg, profile_kwargs, embedder_vecs, expected_len, signal_checks
):
    """Zero/null/absent inputs produce zero or absent output signals."""
    papers = [] if papers_arg == [] else [_make_paper(title="P1", external_id="arxiv:1")]
    profile = _make_profile(**profile_kwargs)
    embedder = _make_embedder(embedder_vecs)

    result = await stage1_embedding_filter(papers, profile, embedder, top_k=10)

    assert len(result) == expected_len
    if expected_len == 0:
        embedder.embed_texts.assert_not_called()
    for key, val in signal_checks.items():
        if key == "__absent__":
            assert val not in result[0].signals
        else:
            assert result[0].signals[key] == val


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
    for key in ("embedding", "topic", "recency", "author_bonus"):
        assert key in signals, f"Missing signal: {key}"
    # M-06: l2_penalty key dropped from signals (math still applied to embedding)
    assert "l2_penalty" not in signals, "l2_penalty key must not be in signals (M-06)"


# ---------------------------------------------------------------------------
# L2 negative-centroid penalty (Wave 1cd §7.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_no_negative_centroid_no_penalty():
    """When profile.negative_centroid is None, no penalty applied to embedding_sim."""
    paper = _make_paper()
    profile = _make_profile(centroid=None, negative_centroid=None)
    embedder = _make_embedder([[1.0, 0.0, 0.0, 0.0]])

    result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    assert len(result) == 1
    assert "l2_penalty" not in result[0].signals


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
    # M-06: l2_penalty key is gone; verify the math is still applied via embedding
    assert "l2_penalty" not in signals
    expected_penalty = l2_lambda * 1.0  # cosine([1,0,0,0], [1,0,0,0]) = 1.0
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
    # cosine([0,1,0,0], [1,0,0,0]) = 0.0 → penalty = 0.0 → embedding unchanged at 0.0
    assert "l2_penalty" not in result[0].signals
    assert abs(result[0].signals["embedding"]) < 1e-6


# ---------------------------------------------------------------------------
# UTC date contract (M-8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_default_now_uses_utc_date():
    """stage1 uses datetime.now(UTC).date() — not the local-timezone date.today().

    We freeze datetime.now(UTC) to an arbitrary UTC instant and verify the
    recency decay is computed against that UTC date.  A naive date.today() call
    would use the local timezone, which on a non-UTC host could produce a
    different calendar date (e.g. one day behind UTC).  The assertion fails if
    the implementation is reverted to date.today().
    """
    # Pin UTC "now" to midnight-plus-one-second on 2025-01-15 UTC.
    # A host running UTC-12 would have date.today() → 2025-01-14 at that instant,
    # producing age_days=1 and a different recency score.
    frozen_utc_dt = datetime(2025, 1, 15, 0, 0, 1, tzinfo=UTC)
    frozen_utc_date = frozen_utc_dt.date()  # 2025-01-15

    # Paper published on the UTC date — age_days should be 0 → recency == 1.0
    paper = PaperCreate(
        external_id="arxiv:utc-test",
        source_type=SourceType.ARXIV,
        title="UTC Test Paper",
        authors=["Author"],
        abstract="abstract",
        published_date=frozen_utc_date,
        url="https://arxiv.org/abs/utc-test",
    )
    profile = _make_profile(centroid=None)
    embedder = _make_embedder([[1.0, 0.0]])

    class _FakeDatetime(datetime):
        """datetime subclass that overrides now() to return frozen_utc_dt."""

        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is UTC:
                return frozen_utc_dt
            return super().now(tz=tz)

    with patch("paper_ingestion.pulse.scoring.datetime", _FakeDatetime):
        result = await stage1_embedding_filter([paper], profile, embedder, top_k=10)

    assert len(result) == 1
    # age_days == 0 → exp(0 / 30) == 1.0
    assert abs(result[0].signals["recency"] - 1.0) < 1e-6, (
        "recency should be 1.0 when published_date matches UTC date; "
        "a date.today() fallback on a non-UTC host would give age_days=1 "
        f"(recency={math.exp(-1 / 30.0):.6f}) instead"
    )


def test_llm_constants_read_from_cfg_at_call_time(monkeypatch):
    """_llm_concurrency() and _llm_model() re-read _get_cfg() on every call."""

    class _FakeCfg:
        def __init__(self, concurrency, model):
            self.pulse_llm_concurrency = concurrency
            self.pulse_stage2_model = model

    calls = iter([_FakeCfg(2, "fast"), _FakeCfg(8, "smart"), _FakeCfg(4, None), _FakeCfg(1, "")])

    monkeypatch.setattr(_scoring_mod, "_get_cfg", lambda: next(calls))

    assert _llm_concurrency() == 2
    assert _llm_model() == "smart"
    assert _llm_concurrency() == 4
    assert _llm_model() == "fast"
