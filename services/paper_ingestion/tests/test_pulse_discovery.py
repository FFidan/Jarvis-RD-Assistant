"""Tests for app.pulse.discovery — discover_candidates orchestration.

TDD: tests written before implementation.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from paper_ingestion.models import PaperCreate, SourceType, TopicRef
from paper_ingestion.pulse.profile import UserProfile
from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile() -> UserProfile:
    return UserProfile(
        topics=[TopicRef(id=1, name="machine learning", query_terms=["ML"])],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={"embedding": 0.35, "topic": 0.25, "recency": 0.15, "author_bonus": 0.25},
        deck_size=10,
        stage2_top_k=30,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )


def _paper(
    external_id: str,
    title: str = "Paper title",
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    source: SourceType = SourceType.ARXIV,
) -> PaperCreate:
    metadata: dict = {}
    if doi:
        metadata["doi"] = doi
    if arxiv_id:
        metadata["arxiv_id"] = arxiv_id
    return PaperCreate(
        external_id=external_id,
        source_type=source,
        title=title,
        authors=["Author A"],
        abstract="Abstract",
        url=f"https://example.com/{external_id}",
        metadata=metadata,
    )


def _source_row(source_type: str, rid: int = 1) -> FakeRecord:
    return FakeRecord(
        {
            "id": rid,
            "source_type": source_type,
            "enabled": True,
            "config": {},
        }
    )


class _StubSource:
    """Fake PaperSource implementation for dependency injection into tests."""

    def __init__(
        self,
        papers: list[PaperCreate] | None = None,
        raises: Exception | None = None,
        diagnostic: dict | None = None,
    ):
        self._papers = papers or []
        self._raises = raises
        self.last_poll_diagnostic = diagnostic
        self.fetch_new_since_calls = 0

    async def fetch_new_since(self, since, topics, limit=100):
        self.fetch_new_since_calls += 1
        if self._raises is not None:
            raise self._raises
        if self._papers:
            self.last_poll_diagnostic = None
        return list(self._papers)


class _UnsupportedSource(_StubSource):
    source_type = "local"
    supports_pulse_polling = False


def _make_source_class(stub: _StubSource):
    """Wrap a stub instance so that calling `cls(config, http_client)` returns it."""

    def factory(config, http_client):
        return stub

    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_calls_every_enabled_source():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("openalex", 2),
        _source_row("pubmed", 3),
    ]

    stubs = {
        "arxiv": _StubSource([_paper("arxiv:0001", "A")]),
        "openalex": _StubSource([_paper("oa:0001", "B")]),
        "pubmed": _StubSource([_paper("pm:0001", "C")]),
    }

    def fake_get(name):
        return _make_source_class(stubs[name])

    http_client = MagicMock()
    profile = _make_profile()

    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, source_counts = await discover_candidates(
            pool, http_client, profile, since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    assert stubs["arxiv"].fetch_new_since_calls == 1
    assert stubs["openalex"].fetch_new_since_calls == 1
    assert stubs["pubmed"].fetch_new_since_calls == 1
    assert len(result) == 3
    assert isinstance(source_counts, dict)  # source_counts keyed by plugin class name


@pytest.mark.asyncio
async def test_graceful_source_failure():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("openalex", 2),
        _source_row("pubmed", 3),
    ]

    stubs = {
        "arxiv": _StubSource([_paper("arxiv:1", "A")]),
        "openalex": _StubSource(raises=RuntimeError("boom")),
        "pubmed": _StubSource([_paper("pm:1", "C")]),
    }

    def fake_get(name):
        return _make_source_class(stubs[name])

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, source_counts = await discover_candidates(
            pool, MagicMock(), profile, since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    ids = {p.external_id for p in result}
    assert "arxiv:1" in ids
    assert "pm:1" in ids
    assert len(result) == 2


@pytest.mark.asyncio
async def test_dedup_by_doi():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("openalex", 1),
        _source_row("pubmed", 2),
    ]

    shared_doi = "10.1234/same"
    stubs = {
        "openalex": _StubSource(
            [_paper("oa:1", "Shared Paper", doi=shared_doi, source=SourceType.OPENALEX)]
        ),
        "pubmed": _StubSource(
            [_paper("pm:1", "Shared Paper", doi=shared_doi, source=SourceType.PUBMED)]
        ),
    }

    def fake_get(name):
        return _make_source_class(stubs[name])

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, _ = await discover_candidates(
            pool, MagicMock(), profile, since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    assert len(result) == 1


@pytest.mark.asyncio
async def test_dedup_by_arxiv_id():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("openalex", 2),
    ]

    stubs = {
        "arxiv": _StubSource([_paper("arxiv:2401.00001", "ArxivHit", arxiv_id="2401.00001")]),
        "openalex": _StubSource(
            [
                _paper(
                    "oa:mirror",
                    "ArxivHit Mirror",
                    arxiv_id="2401.00001",
                    source=SourceType.OPENALEX,
                )
            ]
        ),
    }

    def fake_get(name):
        return _make_source_class(stubs[name])

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, _ = await discover_candidates(
            pool, MagicMock(), profile, since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    assert len(result) == 1
    # First occurrence wins
    assert result[0].external_id == "arxiv:2401.00001"


@pytest.mark.asyncio
async def test_dedup_by_title_hash():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("openalex", 2),
    ]

    stubs = {
        "arxiv": _StubSource([_paper("arxiv:a", "Deep Learning For Science")]),
        "openalex": _StubSource(
            [_paper("oa:b", "  deep learning for science  ", source=SourceType.OPENALEX)]
        ),
    }

    def fake_get(name):
        return _make_source_class(stubs[name])

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, _ = await discover_candidates(
            pool, MagicMock(), profile, since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    assert len(result) == 1
    assert result[0].external_id == "arxiv:a"


@pytest.mark.asyncio
async def test_empty_when_no_enabled_sources():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class") as m:
        result, source_counts = await discover_candidates(
            pool, MagicMock(), profile, since=datetime(2026, 1, 1, tzinfo=UTC)
        )
        m.assert_not_called()

    assert result == []
    assert source_counts == {}


@pytest.mark.asyncio
async def test_unknown_source_class_is_skipped():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("mystery", 2),
    ]

    stubs = {"arxiv": _StubSource([_paper("arxiv:1", "Hit")])}

    def fake_get(name):
        if name == "mystery":
            return None
        return _make_source_class(stubs[name])

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, _ = await discover_candidates(
            pool, MagicMock(), profile, since=datetime(2026, 1, 1, tzinfo=UTC)
        )

    assert len(result) == 1
    assert result[0].external_id == "arxiv:1"


def test_per_source_cap_divides_across_sources():
    """per_source_cap should spread stage2_top_k * 2 across sources, not multiply."""
    import math

    stage2_top_k = 40
    sources = ["arxiv", "s2", "openalex", "pubmed"]
    per_source_cap = max(
        10,
        min(
            stage2_top_k,
            math.ceil(stage2_top_k * 2 / max(1, len(sources))),
        ),
    )
    assert per_source_cap == 20  # 80 / 4 = 20
    assert per_source_cap * len(sources) <= stage2_top_k * 2 + len(sources)  # ~total budget


def test_per_source_cap_floor():
    """Floor of 10 applies when stage2_top_k * 2 / n_sources < 10."""
    import math

    stage2_top_k = 10
    sources = ["arxiv", "s2", "openalex", "pubmed", "biorxiv", "chemrxiv"]  # 6 sources
    per_source_cap = max(
        10,
        min(
            stage2_top_k,
            math.ceil(stage2_top_k * 2 / max(1, len(sources))),
        ),
    )
    # 20 / 6 = 3.33 → ceil = 4, but floor is 10; however cap is also stage2_top_k=10
    # max(10, min(10, 4)) = max(10, 4) = 10
    assert per_source_cap == 10


def test_per_source_cap_single_source():
    """With 1 source, cap is bounded by stage2_top_k (no blowup)."""
    import math

    stage2_top_k = 40
    sources = ["arxiv"]
    per_source_cap = max(
        10,
        min(
            stage2_top_k,
            math.ceil(stage2_top_k * 2 / max(1, len(sources))),
        ),
    )
    # 80 / 1 = 80, but capped at stage2_top_k=40
    assert per_source_cap == 40


@pytest.mark.asyncio
async def test_source_cache_used_when_provided():
    """When source_cache contains a source type, that cached instance is used.

    This verifies the rate-limiter preservation path: if source_cache['arxiv']
    is already populated, discover_candidates uses it instead of instantiating
    a new object (so rate-limiter state carries over between Pulse runs).
    """
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_source_row("arxiv", 1)]

    cached_stub = _StubSource([_paper("arxiv:cached", "Cached Hit")])
    source_cache = {"arxiv": cached_stub}

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class") as m:
        result, _ = await discover_candidates(
            pool,
            MagicMock(),
            profile,
            since=datetime(2026, 1, 1, tzinfo=UTC),
            source_cache=source_cache,
        )
        # get_source_class must NOT be called — the cached instance was used
        m.assert_not_called()

    assert cached_stub.fetch_new_since_calls == 1
    assert len(result) == 1
    assert result[0].external_id == "arxiv:cached"


@pytest.mark.asyncio
async def test_include_diagnostics_reports_rate_limit_and_unsupported_sources():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        _source_row("arxiv", 1),
        _source_row("local", 2),
    ]

    request = httpx.Request("GET", "https://export.arxiv.org/api/query")
    response = httpx.Response(429, headers={"Retry-After": "17"}, request=request)
    arxiv_error = httpx.HTTPStatusError("rate limited", request=request, response=response)
    stubs = {
        "arxiv": _StubSource(raises=arxiv_error),
        "local": _UnsupportedSource(),
    }

    def fake_get(name):
        return _make_source_class(stubs[name])

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, source_counts, diagnostics = await discover_candidates(
            pool,
            MagicMock(),
            profile,
            since=datetime(2026, 1, 1, tzinfo=UTC),
            include_diagnostics=True,
        )

    assert result == []
    assert source_counts == {"_StubSource": 0, "_UnsupportedSource": 0}
    assert diagnostics["_StubSource"]["status"] == "rate_limit"
    assert diagnostics["_StubSource"]["retry_after_s"] == 17
    assert diagnostics["_UnsupportedSource"]["status"] == "unsupported"


@pytest.mark.asyncio
async def test_include_diagnostics_redacts_raw_non_rate_limit_exception_text():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_source_row("arxiv", 1)]

    request = httpx.Request(
        "GET",
        "https://export.arxiv.org/api/query?search_query=secret-topic&api_key=token123",
    )
    arxiv_error = httpx.ReadTimeout(
        "timed out fetching https://export.arxiv.org/api/query?api_key=token123",
        request=request,
    )
    stubs = {"arxiv": _StubSource(raises=arxiv_error)}

    def fake_get(name):
        return _make_source_class(stubs[name])

    profile = _make_profile()
    with patch("paper_ingestion.pulse.discovery.get_source_class", side_effect=fake_get):
        result, source_counts, diagnostics = await discover_candidates(
            pool,
            MagicMock(),
            profile,
            since=datetime(2026, 1, 1, tzinfo=UTC),
            include_diagnostics=True,
        )

    assert result == []
    assert source_counts == {"_StubSource": 0}
    diagnostic = diagnostics["_StubSource"]
    assert diagnostic["status"] == "error"
    assert "_StubSource request failed" in diagnostic["message"]
    assert "token123" not in str(diagnostic)
    assert "secret-topic" not in str(diagnostic)
    assert "export.arxiv.org/api/query" not in str(diagnostic)


@pytest.mark.asyncio
async def test_include_diagnostics_prefers_source_poll_diagnostic_for_empty_result():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_source_row("arxiv", 1)]

    stub = _StubSource(
        diagnostic={
            "status": "rate_limit",
            "message": "arXiv rate limit reached. Retry later.",
            "status_code": 429,
            "retry_after_s": 60,
            "settings_hint": None,
        }
    )

    with patch(
        "paper_ingestion.pulse.discovery.get_source_class",
        return_value=_make_source_class(stub),
    ):
        result, source_counts, diagnostics = await discover_candidates(
            pool,
            MagicMock(),
            _make_profile(),
            since=datetime(2026, 1, 1, tzinfo=UTC),
            include_diagnostics=True,
        )

    assert result == []
    assert source_counts == {"_StubSource": 0}
    assert diagnostics["_StubSource"]["status"] == "rate_limit"
    assert diagnostics["_StubSource"]["status_code"] == 429


@pytest.mark.asyncio
async def test_include_diagnostics_clears_stale_source_diagnostic_after_success():
    from paper_ingestion.pulse.discovery import discover_candidates

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_source_row("arxiv", 1)]

    stub = _StubSource(
        papers=[_paper("arxiv:recovered", "Recovered")],
        diagnostic={
            "status": "rate_limit",
            "message": "old throttle",
            "status_code": 429,
            "retry_after_s": 60,
            "settings_hint": None,
        },
    )

    with patch(
        "paper_ingestion.pulse.discovery.get_source_class",
        return_value=_make_source_class(stub),
    ):
        result, _source_counts, diagnostics = await discover_candidates(
            pool,
            MagicMock(),
            _make_profile(),
            since=datetime(2026, 1, 1, tzinfo=UTC),
            include_diagnostics=True,
        )

    assert [paper.external_id for paper in result] == ["arxiv:recovered"]
    assert diagnostics["_StubSource"]["status"] == "ok"
