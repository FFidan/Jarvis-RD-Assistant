"""Graceful-degradation tests for the Pulse pipeline.

These tests ensure that individual source failures, LLM timeouts, and empty
discovery runs NEVER raise up through ``run_pulse`` — the service must
produce diagnostic stats and, whenever possible, a deck.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from paper_ingestion.models import PaperCreate, SourceType, TopicRef
from paper_ingestion.pulse.profile import UserProfile
from tests.conftest import FakeRecord, _make_pool_and_conn


def _profile() -> UserProfile:
    return UserProfile(
        topics=[TopicRef(id=1, name="ml", query_terms=["ML"])],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
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


def _source_row(st: str, rid: int = 1, enabled: bool = True) -> FakeRecord:
    return FakeRecord({"id": rid, "source_type": st, "enabled": enabled, "config": {}})


class _Src:
    def __init__(self, papers=None, raises=None):
        self._papers = papers or []
        self._raises = raises

    async def fetch_new_since(self, since, topics, limit=100, user_id=None):
        if self._raises:
            raise self._raises
        return list(self._papers)


def _cls(src):
    def make(config, http_client, db_pool=None):
        return src

    return make


# ---------------------------------------------------------------------------
# Discovery-level degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_openalex_key_baseline_arxiv_only():
    """paper_sources only returns enabled rows, so disabled openalex is excluded."""
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_source_row("arxiv", 1)]  # openalex NOT present

    arxiv_stub = _Src([_paper(1)])

    def fake_get(name):
        return _cls(arxiv_stub) if name == "arxiv" else None

    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, _, _ = await discover_candidates(
            pool, MagicMock(), _profile(), since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    assert len(result) == 1
    assert result[0].external_id == "arxiv:0001"


@pytest.mark.asyncio
async def test_s2_rate_limited_skipped():
    """HTTP 429 from Semantic Scholar must not break discovery."""
    from paper_ingestion.pulse.discovery import discover_candidates

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

    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, _, _ = await discover_candidates(
            pool, MagicMock(), _profile(), since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    # Arxiv result survives, S2 skipped
    assert len(result) == 1
    assert result[0].external_id == "arxiv:0001"


@pytest.mark.asyncio
async def test_openalex_5xx_skipped():
    from paper_ingestion.pulse.discovery import discover_candidates

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

    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, _, _ = await discover_candidates(
            pool, MagicMock(), _profile(), since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert len(result) == 1
