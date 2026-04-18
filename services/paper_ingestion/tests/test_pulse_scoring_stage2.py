"""Tests for app.pulse.scoring — stage2_llm_rerank.

TDD: tests written before implementation.
Uses respx to mock the httpx client.
"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

try:
    import importlib.util

    _HAS_RESPX = importlib.util.find_spec("respx") is not None
except Exception:
    _HAS_RESPX = False

from app.models import PaperCreate, SourceType, TopicRef
from app.pulse.profile import UserProfile
from app.pulse.scoring import ScoredCandidate, stage2_llm_rerank

from tests.conftest import fake_llm_score_response

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


def _make_litellm_response(relevance: int = 7, novelty: int = 5) -> dict:
    """Minimal LiteLLM-style response payload."""
    return {
        "choices": [
            {"message": {"content": fake_llm_score_response(relevance=relevance, novelty=novelty)}}
        ]
    }


# ---------------------------------------------------------------------------
# Mock-based tests (no respx required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_fills_llm_scores():
    """All candidates get llm_relevance and llm_novelty filled after stage2."""
    papers = [_make_paper(i) for i in range(3)]
    stage1_out = [_make_scored(p) for p in papers]
    profile = _make_profile()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _make_litellm_response(relevance=8, novelty=6)
    http_client.post.return_value = mock_resp

    result = await stage2_llm_rerank(stage1_out, profile, http_client)

    assert len(result) == 3
    for sc in result:
        assert sc.llm_relevance is not None
        assert sc.llm_novelty is not None
        assert isinstance(sc.reasoning, str)
        assert len(sc.reasoning) > 0


@pytest.mark.asyncio
async def test_stage2_normalizes_scores_to_0_1():
    """llm_relevance and llm_novelty signals are normalized to [0,1] (value/10)."""
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _make_litellm_response(relevance=8, novelty=6)
    http_client.post.return_value = mock_resp

    result = await stage2_llm_rerank(stage1_out, profile, http_client)

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

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = RuntimeError("LLM unavailable")

    result = await stage2_llm_rerank(stage1_out, profile, http_client)

    assert len(result) == 1
    assert result[0].llm_relevance is None
    assert result[0].llm_novelty is None
    assert result[0].reasoning == "LLM scoring failed"


@pytest.mark.asyncio
async def test_stage2_graceful_json_parse_error():
    """When LLM returns malformed JSON, candidate gets None scores gracefully."""
    paper = _make_paper(0)
    stage1_out = [_make_scored(paper)]
    profile = _make_profile()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"choices": [{"message": {"content": "not valid json {{"}}]}
    http_client.post.return_value = mock_resp

    result = await stage2_llm_rerank(stage1_out, profile, http_client)

    assert result[0].llm_relevance is None
    assert result[0].llm_novelty is None
    assert result[0].reasoning == "LLM scoring failed"


@pytest.mark.asyncio
async def test_stage2_preserves_stage1_signals():
    """Stage 2 preserves existing stage1 signals and adds llm_ signals."""
    paper = _make_paper(0)
    sc = _make_scored(paper, embedding=0.75)
    profile = _make_profile()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _make_litellm_response(relevance=9, novelty=7)
    http_client.post.return_value = mock_resp

    result = await stage2_llm_rerank([sc], profile, http_client)

    assert result[0].signals["embedding"] == 0.75
    assert "llm_relevance" in result[0].signals
    assert "llm_novelty" in result[0].signals


@pytest.mark.asyncio
async def test_stage2_concurrency_limit():
    """In-flight LLM calls do not exceed the concurrency limit (≤5 simultaneous)."""
    num_candidates = 10
    papers = [_make_paper(i) for i in range(num_candidates)]
    stage1_out = [_make_scored(p) for p in papers]
    profile = _make_profile()

    in_flight = []
    max_in_flight = [0]
    call_count = [0]

    async def mock_post(*args, **kwargs):
        call_count[0] += 1
        in_flight.append(1)
        max_in_flight[0] = max(max_in_flight[0], len(in_flight))
        await asyncio.sleep(0.01)  # simulate latency
        in_flight.pop()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = _make_litellm_response(7, 5)
        return mock_resp

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = mock_post

    result = await stage2_llm_rerank(stage1_out, profile, http_client)

    assert len(result) == num_candidates
    assert call_count[0] == num_candidates
    # Max concurrency should be ≤5
    assert max_in_flight[0] <= 5


@pytest.mark.asyncio
async def test_stage2_empty_input_returns_empty():
    """Empty stage1_out returns empty list."""
    profile = _make_profile()
    http_client = AsyncMock(spec=httpx.AsyncClient)

    result = await stage2_llm_rerank([], profile, http_client)

    assert result == []
    http_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_stage2_partial_failure_others_succeed():
    """When one LLM call fails, remaining candidates still get scored."""
    papers = [_make_paper(i) for i in range(3)]
    stage1_out = [_make_scored(p) for p in papers]
    profile = _make_profile()

    call_n = [0]

    async def mock_post(*args, **kwargs):
        call_n[0] += 1
        if call_n[0] == 2:
            raise RuntimeError("LLM error on call 2")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = _make_litellm_response(7, 5)
        return mock_resp

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = mock_post

    result = await stage2_llm_rerank(stage1_out, profile, http_client)

    assert len(result) == 3
    scored = [sc for sc in result if sc.llm_relevance is not None]
    failed = [sc for sc in result if sc.llm_relevance is None]
    assert len(scored) == 2
    assert len(failed) == 1
    assert failed[0].reasoning == "LLM scoring failed"


@pytest.mark.asyncio
async def test_stage2_falls_back_on_llm_error():
    """When call_llm raises RuntimeError, fallback returns stage1 signals."""
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
    http_client = AsyncMock(spec=httpx.AsyncClient)

    with patch(
        "app.pulse.scoring.call_llm",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LiteLLM unavailable"),
    ):
        result = await stage2_llm_rerank([sc], profile, http_client)

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
