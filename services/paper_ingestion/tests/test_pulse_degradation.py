"""Graceful-degradation tests for the Pulse pipeline.

These tests ensure that individual source failures, LLM timeouts, and empty
discovery runs NEVER raise up through ``run_pulse`` — the service must
produce diagnostic stats and, whenever possible, a deck.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.models import PaperCreate, SourceType, TopicRef
from app.pulse.profile import UserProfile
from app.pulse.scoring import ScoredCandidate

from tests.conftest import FakeRecord, _make_pool_and_conn


def _profile() -> UserProfile:
    return UserProfile(
        topics=[TopicRef(id=1, name="ml", query_terms=["ML"])],
        tracked_author_ids=[],
        library_centroid=None,
        weights={"embedding": 0.35, "topic": 0.25, "recency": 0.15, "author_bonus": 0.25},
        deck_size=5,
        stage2_top_k=10,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )


def _paper(idx: int) -> PaperCreate:
    return PaperCreate(
        external_id=f"arxiv:{idx:04d}",
        source_type=SourceType.ARXIV,
        title=f"Paper {idx}",
        authors=["A"],
        abstract="abc",
        url=f"https://arxiv.org/abs/{idx:04d}",
    )


def _scored(idx: int) -> ScoredCandidate:
    return ScoredCandidate(
        paper=_paper(idx),
        signals={"embedding": 0.5, "topic": 0.5, "recency": 0.5, "author_bonus": 0.0},
        llm_relevance=7,
        llm_novelty=5,
        reasoning="rel",
        final_score=0.6,
    )


def _source_row(st: str, rid: int = 1, enabled: bool = True) -> FakeRecord:
    return FakeRecord({"id": rid, "source_type": st, "enabled": enabled, "config": {}})


class _Src:
    def __init__(self, papers=None, raises=None):
        self._papers = papers or []
        self._raises = raises

    async def fetch_new_since(self, since, topics, limit=100):
        if self._raises:
            raise self._raises
        return list(self._papers)


def _cls(src):
    def make(config, http_client):
        return src

    return make


# ---------------------------------------------------------------------------
# Discovery-level degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_openalex_key_baseline_arxiv_only():
    """paper_sources only returns enabled rows, so disabled openalex is excluded."""
    from app.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_source_row("arxiv", 1)]  # openalex NOT present

    arxiv_stub = _Src([_paper(1)])

    def fake_get(name):
        return _cls(arxiv_stub) if name == "arxiv" else None

    with patch("app.pulse.discovery.get_source_class", side_effect=fake_get):
        result = await discover_candidates(
            pool, MagicMock(), _profile(), since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    assert len(result) == 1
    assert result[0].external_id == "arxiv:0001"


@pytest.mark.asyncio
async def test_s2_rate_limited_skipped():
    """HTTP 429 from Semantic Scholar must not break discovery."""
    from app.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("semantic_scholar", 2),
    ]

    request = httpx.Request("GET", "https://api.semanticscholar.org/x")
    response = httpx.Response(429, request=request)
    stubs = {
        "arxiv": _Src([_paper(1)]),
        "semantic_scholar": _Src(
            raises=httpx.HTTPStatusError("429", request=request, response=response)
        ),
    }

    def fake_get(name):
        return _cls(stubs[name])

    with patch("app.pulse.discovery.get_source_class", side_effect=fake_get):
        result = await discover_candidates(
            pool, MagicMock(), _profile(), since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    # Arxiv result survives, S2 skipped
    assert len(result) == 1
    assert result[0].external_id == "arxiv:0001"


@pytest.mark.asyncio
async def test_openalex_5xx_skipped():
    from app.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("openalex", 2),
    ]
    stubs = {
        "arxiv": _Src([_paper(1)]),
        "openalex": _Src(raises=Exception("503 Service Unavailable")),
    }

    def fake_get(name):
        return _cls(stubs[name])

    with patch("app.pulse.discovery.get_source_class", side_effect=fake_get):
        result = await discover_candidates(
            pool, MagicMock(), _profile(), since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Job-level degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_timeout_deck_still_produced():
    from app.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()

    async def timeout(*a, **kw):
        raise TimeoutError()

    stage1_out = [_scored(i) for i in range(3)]
    stage3_out = [_scored(i) for i in range(3)]

    with (
        patch("app.pulse.job.load_profile", AsyncMock(return_value=_profile())),
        patch(
            "app.pulse.job.discover_candidates",
            AsyncMock(return_value=[_paper(i) for i in range(3)]),
        ),
        patch(
            "app.pulse.job.stage1_embedding_filter",
            AsyncMock(return_value=stage1_out),
        ),
        patch("app.pulse.job.stage2_llm_rerank", side_effect=timeout),
        patch(
            "app.pulse.job.stage3_combine",
            AsyncMock(return_value=stage3_out),
        ),
        patch(
            "app.pulse.job.assemble_deck",
            AsyncMock(return_value=stage3_out),
        ),
        patch("app.pulse.job.upsert_paper", AsyncMock()),
        patch("app.pulse.job.persist_deck", AsyncMock(return_value=77)) as p_persist,
    ):
        stats = await run_pulse(pool, MagicMock(), MagicMock())

    assert stats["last_error"] is not None
    assert "timeout" in str(stats["last_error"]).lower()
    p_persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_discovery_empty_deck_not_error():
    from app.pulse.job import run_pulse

    pool, _conn = _make_pool_and_conn()

    with (
        patch("app.pulse.job.load_profile", AsyncMock(return_value=_profile())),
        patch("app.pulse.job.discover_candidates", AsyncMock(return_value=[])),
        patch("app.pulse.job.stage1_embedding_filter", AsyncMock(return_value=[])),
        patch("app.pulse.job.stage2_llm_rerank", AsyncMock(return_value=[])),
        patch("app.pulse.job.stage3_combine", AsyncMock(return_value=[])),
        patch("app.pulse.job.assemble_deck", AsyncMock(return_value=[])),
        patch("app.pulse.job.upsert_paper", AsyncMock()),
        patch("app.pulse.job.persist_deck", AsyncMock(return_value=1)) as p_persist,
    ):
        stats = await run_pulse(pool, MagicMock(), MagicMock())

    assert stats["candidate_count"] == 0
    assert stats["last_error"] is None
    p_persist.assert_awaited_once()
