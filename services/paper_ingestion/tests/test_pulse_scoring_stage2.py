"""Tests for app.pulse.scoring — stage2_llm_rerank.

TDD: tests written before implementation.
Uses mocked call_llm_structured (Instructor-based structured output).
"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import importlib.util

    _HAS_RESPX = importlib.util.find_spec("respx") is not None
except Exception:
    _HAS_RESPX = False

from jarvis_common.verify import QuoteVerifier
from paper_ingestion.models import PaperCreate, SourceType, TopicRef
from paper_ingestion.pulse.models import PulseScoringOutput
from paper_ingestion.pulse.profile import UserProfile
from paper_ingestion.pulse.scoring import (
    ScoredCandidate,
    stage2_llm_rerank,
)


def _make_verifier() -> QuoteVerifier:
    """Return a real QuoteVerifier for tests that need mandatory verifier."""
    return QuoteVerifier()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paper(idx: int = 0) -> PaperCreate:
    return PaperCreate(
        external_id=f"arxiv:{idx:04d}",
        source_type=SourceType.ARXIV,
        title=f"Test Paper {idx}",
        authors=["Author A"],
        abstract=f"Abstract for paper {idx}.",
        published_date=date.today(),
        url=f"https://arxiv.org/abs/{idx:04d}",
    )


def _make_scored(paper: PaperCreate, embedding: float = 0.5) -> ScoredCandidate:
    return ScoredCandidate(
        paper=paper,
        signals={"embedding": embedding, "topic": 0.4, "recency": 0.9, "author_bonus": 0.0},
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=None,
    )


def _make_profile() -> UserProfile:
    return UserProfile(
        topics=[TopicRef(id=1, name="Neural ODEs", description="Continuous dynamics")],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={"embedding": 0.2, "topic": 0.2, "llm_relevance": 0.3, "llm_novelty": 0.1},
        deck_size=10,
        stage2_top_k=50,
        recent_positive_titles=["Great Paper"],
        recent_negative_titles=["Bad Paper"],
    )


def _make_scoring_output(relevance: int = 7, novelty: int = 5) -> PulseScoringOutput:
    return PulseScoringOutput(relevance=relevance, novelty=novelty, reasoning="test reasoning")


# ---------------------------------------------------------------------------
# Mock-based tests (no respx required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_fills_llm_scores():
    """All candidates get llm_relevance and llm_novelty filled after stage2."""
    papers = [_make_paper(i) for i in range(3)]
    stage1_out = [_make_scored(p) for p in papers]
    profile = _make_profile()

    mock_openai_client = MagicMock()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_scoring_output(relevance=8, novelty=6),
    ):
        result = await stage2_llm_rerank(
            stage1_out,
            profile,
            verifier=_make_verifier(),
            openai_client=mock_openai_client,
        )

    assert len(result) == 3
    for sc in result:
        assert sc.llm_relevance is not None
        assert sc.llm_novelty is not None
        assert isinstance(sc.reasoning, str)
        assert len(sc.reasoning) > 0


@pytest.mark.asyncio
async def test_stage2_uses_fast_model_and_single_retry_by_default(monkeypatch):
    """Pulse Stage 2 defaults to the faster local scoring alias and reduced retries."""
    import importlib

    import paper_ingestion.pulse.scoring as scoring_mod

    monkeypatch.delenv("PULSE_STAGE2_MODEL", raising=False)
    monkeypatch.delenv("PULSE_STAGE2_MAX_RETRIES", raising=False)
    importlib.reload(scoring_mod)

    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_scoring_output(relevance=8, novelty=6),
    ) as call_llm:
        await scoring_mod.stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert call_llm.await_args is not None
    call_kwargs = call_llm.await_args.kwargs
    assert call_kwargs["options"].model == "fast"
    assert call_kwargs["max_retries"] == 1


@pytest.mark.asyncio
async def test_stage2_model_and_retry_budget_are_env_configurable(monkeypatch):
    """Operators can trade Pulse quality/speed without code changes."""
    import importlib

    import paper_ingestion.pulse.scoring as scoring_mod

    monkeypatch.setenv("PULSE_STAGE2_MODEL", "smart")
    monkeypatch.setenv("PULSE_STAGE2_MAX_RETRIES", "0")
    importlib.reload(scoring_mod)

    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_scoring_output(relevance=8, novelty=6),
    ) as call_llm:
        await scoring_mod.stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert call_llm.await_args is not None
    call_kwargs = call_llm.await_args.kwargs
    assert call_kwargs["options"].model == "smart"
    assert call_kwargs["max_retries"] == 0


@pytest.mark.asyncio
async def test_stage2_normalizes_scores_to_0_1():
    """llm_relevance and llm_novelty signals are normalized to [0,1] (value/10)."""
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    mock_openai_client = MagicMock()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_scoring_output(relevance=8, novelty=6),
    ):
        result = await stage2_llm_rerank(
            stage1_out,
            profile,
            verifier=_make_verifier(),
            openai_client=mock_openai_client,
        )

    assert result[0].llm_relevance == 8
    assert result[0].llm_novelty == 6
    # signals should be normalized
    assert abs(result[0].signals["llm_relevance"] - 0.8) < 1e-6
    assert abs(result[0].signals["llm_novelty"] - 0.6) < 1e-6


@pytest.mark.asyncio
async def test_stage2_graceful_llm_exception():
    """When LLM raises an exception, candidate gets None scores + 'LLM scoring failed' reasoning."""
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM unavailable"),
    ):
        result = await stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert len(result) == 1
    assert result[0].llm_relevance is None
    assert result[0].llm_novelty is None
    assert result[0].reasoning == "LLM scoring failed"


@pytest.mark.asyncio
async def test_stage2_graceful_json_parse_error():
    """When call_llm_structured raises ValueError, candidate gets None scores gracefully."""
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        side_effect=ValueError("Invalid structured output"),
    ):
        result = await stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert result[0].llm_relevance is None
    assert result[0].llm_novelty is None
    assert result[0].reasoning == "LLM scoring failed"


@pytest.mark.asyncio
async def test_stage2_preserves_stage1_signals():
    """Stage 2 preserves existing stage1 signals and adds llm_ signals."""
    paper = _make_paper(0)
    sc = _make_scored(paper, embedding=0.75)
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        return_value=_make_scoring_output(relevance=9, novelty=7),
    ):
        result = await stage2_llm_rerank(
            [sc], profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert result[0].signals["embedding"] == 0.75
    assert "llm_relevance" in result[0].signals
    assert "llm_novelty" in result[0].signals


@pytest.mark.asyncio
async def test_stage2_concurrency_limit():
    """In-flight LLM calls do not exceed the concurrency limit."""
    num_candidates = 10
    papers = [_make_paper(i) for i in range(num_candidates)]
    stage1_out = [_make_scored(p) for p in papers]
    profile = _make_profile()

    in_flight = []
    max_in_flight = [0]
    call_count = [0]

    async def mock_call(*args, **kwargs):
        call_count[0] += 1
        in_flight.append(1)
        max_in_flight[0] = max(max_in_flight[0], len(in_flight))
        await asyncio.sleep(0.01)  # simulate latency
        in_flight.pop()
        return _make_scoring_output(7, 5)

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        side_effect=mock_call,
    ):
        result = await stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert len(result) == num_candidates
    assert call_count[0] == num_candidates
    # Max concurrency should be ≤8
    assert max_in_flight[0] <= 8


@pytest.mark.asyncio
async def test_stage2_empty_input_returns_empty():
    """Empty stage1_out returns empty list."""
    profile = _make_profile()

    result = await stage2_llm_rerank(
        [], profile, verifier=_make_verifier(), openai_client=MagicMock()
    )

    assert result == []


@pytest.mark.asyncio
async def test_stage2_partial_failure_others_succeed():
    """When one LLM call fails, remaining candidates still get scored."""
    papers = [_make_paper(i) for i in range(3)]
    stage1_out = [_make_scored(p) for p in papers]
    profile = _make_profile()

    call_n = [0]

    async def mock_call(*args, **kwargs):
        call_n[0] += 1
        if call_n[0] == 2:
            raise RuntimeError("LLM error on call 2")
        return _make_scoring_output(7, 5)

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        side_effect=mock_call,
    ):
        result = await stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert len(result) == 3
    scored = [sc for sc in result if sc.llm_relevance is not None]
    failed = [sc for sc in result if sc.llm_relevance is None]
    assert len(scored) == 2
    assert len(failed) == 1
    assert failed[0].reasoning == "LLM scoring failed"


@pytest.mark.asyncio
async def test_stage2_falls_back_on_llm_error():
    """When call_llm_structured raises RuntimeError, fallback returns stage1 signals."""
    paper = _make_paper(0)
    stage1_signals = {"embedding": 0.6, "topic": 0.3, "recency": 0.8, "author_bonus": 0.0}
    sc = ScoredCandidate(
        paper=paper,
        signals=stage1_signals,
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=0.7,
    )
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LiteLLM unavailable"),
    ):
        result = await stage2_llm_rerank(
            [sc], profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert len(result) == 1
    out = result[0]
    # llm scores are absent — graceful fallback
    assert out.llm_relevance is None
    assert out.llm_novelty is None
    assert out.reasoning == "LLM scoring failed"
    # stage1 signals are preserved unchanged
    assert out.signals == stage1_signals
    # final_score is preserved from stage1
    assert out.final_score == 0.7


@pytest.mark.asyncio
async def test_stage2_valid_json_missing_keys_graceful_fallback():
    """call_llm_structured raises ValueError → graceful fallback.

    After migrating to Instructor, parsing errors surface as ValueError.
    stage2 must catch these and degrade gracefully rather than crash.
    """
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper, embedding=0.6)]
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        side_effect=ValueError("Instructor could not parse LLM output"),
    ):
        result = await stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    assert len(result) == 1
    assert result[0].llm_relevance is None
    assert result[0].llm_novelty is None
    assert result[0].reasoning == "LLM scoring failed"


@pytest.mark.asyncio
async def test_stage2_field_validators_enforce_range():
    """Pydantic Field(ge=1, le=10) replaces manual clamping — values in range pass directly."""
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    with patch(
        "paper_ingestion.pulse.scoring.call_llm_structured",
        new_callable=AsyncMock,
        return_value=PulseScoringOutput(relevance=10, novelty=1, reasoning="extreme values"),
    ):
        result = await stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=MagicMock()
        )

    # Pydantic-validated values are already in [1,10] — no clamping needed
    assert result[0].llm_relevance == 10
    assert result[0].llm_novelty == 1
    assert 0.0 <= result[0].signals["llm_relevance"] <= 1.0
    assert 0.0 <= result[0].signals["llm_novelty"] <= 1.0


# ---------------------------------------------------------------------------
# negative_topics / negative_authors forwarding (Wave 1cd)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_passes_negative_topics_and_authors_to_prompt():
    """stage2_llm_rerank must forward profile.negative_topics and profile.negative_authors
    to build_scoring_prompt so the LLM is aware of rejected signals.

    Uses a profile with non-empty negative_topics/negative_authors and patches
    build_scoring_prompt to capture the kwargs it receives.
    """
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]

    profile = UserProfile(
        topics=[TopicRef(id=1, name="Neural ODEs", description="Continuous dynamics")],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={"embedding": 0.2, "topic": 0.2, "llm_relevance": 0.3, "llm_novelty": 0.1},
        deck_size=10,
        stage2_top_k=50,
        recent_positive_titles=["Good Paper"],
        recent_negative_titles=["Bad Paper"],
        negative_topics=["Computer Vision", "NLP"],
        negative_authors=["Spam Author"],
    )

    captured_kwargs: dict = {}

    # Patch build_scoring_prompt to record its kwargs and return a minimal message list.
    with patch(
        "paper_ingestion.pulse.scoring.build_scoring_prompt",
        wraps=None,
        side_effect=lambda *args, **kwargs: (
            captured_kwargs.update(kwargs)
            or [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "score this paper"},
            ]
        ),
    ):
        with patch(
            "paper_ingestion.pulse.scoring.call_llm_structured",
            new_callable=AsyncMock,
            return_value=PulseScoringOutput(relevance=7, novelty=5, reasoning="relevant"),
        ):
            result = await stage2_llm_rerank(
                stage1_out,
                profile,
                verifier=_make_verifier(),
                openai_client=MagicMock(),
            )

    assert len(result) == 1
    assert result[0].llm_relevance == 7
    assert "negative_topics" in captured_kwargs, (
        "build_scoring_prompt not called with negative_topics"
    )
    assert "negative_authors" in captured_kwargs, (
        "build_scoring_prompt not called with negative_authors"
    )
    assert list(captured_kwargs["negative_topics"]) == ["Computer Vision", "NLP"]
    assert list(captured_kwargs["negative_authors"]) == ["Spam Author"]


@pytest.mark.asyncio
async def test_stage2_raises_sentinel_when_openai_client_none():
    """stage2_llm_rerank raises Stage2ClientUnavailableError (not silent fallback) when openai_client=None.

    W3-DRY-3: The caller (run_pulse) is responsible for logging + degraded-marking,
    so the function must raise an explicit sentinel rather than silently returning stage1 output.

    Note: import inside the function body to survive importlib.reload() in sibling tests
    that reload paper_ingestion.pulse.scoring (which creates a new class object).
    """
    import paper_ingestion.pulse.scoring as _scoring

    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    with pytest.raises(_scoring.Stage2ClientUnavailableError):
        await _scoring.stage2_llm_rerank(
            stage1_out, profile, verifier=_make_verifier(), openai_client=None
        )
