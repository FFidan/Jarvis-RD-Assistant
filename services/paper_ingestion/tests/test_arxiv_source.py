"""Tests for ArxivSource PR-B1 additions:
- consolidate_topics (merge + split at 1500-char cap)
- fetch_new_since wired with PersistentSourceRateLimiter
- source_run_history writes on success and 429
- DRY-S3: pdf_url allowlist check in _parse_entry
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree.ElementTree import fromstring

import httpx
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.arxiv_source import ARXIV_API_URL, ATOM_NS, ArxivSource
from paper_ingestion.sources.base import SourceQuery

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(db_pool=None, config_extra: dict | None = None) -> ArxivSource:
    """Create an ArxivSource with a minimal PaperSourceConfig."""
    config = PaperSourceConfig(
        id=1,
        source_type=SourceType.ARXIV,
        enabled=True,
        config=config_extra or {},
    )
    client = httpx.AsyncClient()
    return ArxivSource(config, client, db_pool=db_pool)


def _make_topic(name: str, terms: list[str] | None = None, idx: int = 1) -> TopicRef:
    return TopicRef(id=idx, name=name, query_terms=terms or [name])


def _fixture_xml() -> bytes:
    return (FIXTURES / "arxiv_new_since.xml").read_bytes()


def _empty_xml() -> bytes:
    """Minimal valid Atom feed with no entries."""
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <totalResults xmlns="http://a9.com/-/spec/opensearch/1.1/">0</totalResults>
</feed>"""


# ---------------------------------------------------------------------------
# Task B: consolidate_topics tests
# ---------------------------------------------------------------------------


def test_consolidate_topics_or_merges_within_1500_chars():
    """All topics are merged into a single SourceQuery when under 1500 chars."""
    source = _make_source()
    topics = [
        _make_topic("neural ODE", ["neural ODE"], 1),
        _make_topic("transformer", ["transformer"], 2),
        _make_topic("diffusion models", ["diffusion models"], 3),
    ]
    result = source.consolidate_topics(topics)

    assert len(result) == 1
    assert isinstance(result[0], SourceQuery)
    # All topics should be included (same elements, order-independent)
    assert sorted(result[0].topics, key=lambda t: t.id) == sorted(topics, key=lambda t: t.id)
    sq = result[0].extra_params["search_query"]
    # Verify OR structure
    assert "neural ODE" in sq
    assert "transformer" in sq
    assert "diffusion models" in sq
    assert " OR " in sq
    # Under 1500 chars
    assert len(sq) <= 1500


def test_consolidate_topics_splits_when_query_too_long():
    """Topics are split into 2 bins when combined query would exceed 1500 chars."""
    source = _make_source()
    # Create many topics whose combined query exceeds 1500 chars.
    # Each term like "neural network topic XX" → roughly 50 chars per part
    # 40 topics × ~55 chars = ~2200 chars total → forces split past 1500-char cap
    topics = [
        _make_topic(f"neural network topic {i:02d}", [f"neural network topic {i:02d}"], i)
        for i in range(40)
    ]
    result = source.consolidate_topics(topics)

    assert len(result) == 2, f"Expected 2 bins, got {len(result)}"
    # Both bins are SourceQuery instances
    for sq in result:
        assert isinstance(sq, SourceQuery)
        assert len(sq.extra_params["search_query"]) <= 1500
    # All topics are covered (union of both bins == all topics)
    covered = set()
    for sq in result:
        for t in sq.topics:
            covered.add(t.id)
    assert covered == {t.id for t in topics}


def test_consolidate_topics_empty_returns_empty():
    """Empty topic list → empty result."""
    source = _make_source()
    assert source.consolidate_topics([]) == []


def test_consolidate_topics_single_topic():
    """Single topic → one SourceQuery."""
    source = _make_source()
    t = _make_topic("ML", ["machine learning"])
    result = source.consolidate_topics([t])
    assert len(result) == 1
    assert result[0].topics == [t]


def test_consolidate_topics_is_deterministic():
    """Same topics input → identical output on repeated calls."""
    source = _make_source()
    topics = [_make_topic(f"topic {i}", [f"term {i}"], i) for i in range(5)]
    r1 = source.consolidate_topics(topics)
    r2 = source.consolidate_topics(topics)
    assert r1 == r2


# ---------------------------------------------------------------------------
# Task B: fetch_new_since — PersistentSourceRateLimiter integration
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_calls_persistent_limiter_acquire_before_http():
    """PersistentSourceRateLimiter.acquire() is called before the HTTP request."""
    fixture = _fixture_xml()
    route = respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    mock_pool = MagicMock()
    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    source = _make_source(db_pool=mock_pool)
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE", ["neural ODE"])]

    acquire_called_before_http: list[bool] = []

    async def _track_acquire():
        acquire_called_before_http.append(route.call_count == 0)

    mock_limiter.acquire.side_effect = _track_acquire

    with patch(
        "paper_ingestion.sources.arxiv_source.PersistentSourceRateLimiter",
        return_value=mock_limiter,
    ):
        await source.fetch_new_since(since=since, topics=topics, limit=10)

    mock_limiter.acquire.assert_called_once()
    # acquire was called before HTTP went out
    assert acquire_called_before_http == [True], "acquire() must fire before the HTTP request"


@respx.mock
async def test_fetch_new_since_no_persistent_limiter_when_no_pool():
    """No PersistentSourceRateLimiter is created when db_pool is None."""
    fixture = _fixture_xml()
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source(db_pool=None)
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML", ["machine learning"])]

    with patch("paper_ingestion.sources.arxiv_source.PersistentSourceRateLimiter") as mock_cls:
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    mock_cls.assert_not_called()
    assert isinstance(papers, list)


# ---------------------------------------------------------------------------
# Task B: fetch_new_since — source_run_history writes
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_writes_run_history_on_success():
    """A successful fetch inserts a row into source_run_history with status='ok'."""
    fixture = _fixture_xml()
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    # Set up a mock pool that records execute calls
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    source = _make_source(db_pool=mock_pool)
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE", ["neural ODE"])]

    with patch(
        "paper_ingestion.sources.arxiv_source.PersistentSourceRateLimiter",
        return_value=mock_limiter,
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert len(papers) > 0

    # _insert_run_history should have been called; check execute was called
    assert mock_conn.execute.called
    # LG-B3 adds a log_event call after _insert_run_history; search all calls.
    all_calls = mock_conn.execute.call_args_list
    run_history_calls = [c for c in all_calls if "source_run_history" in c.args[0]]
    assert run_history_calls, "Expected at least one execute call to source_run_history"
    sql, *args = run_history_calls[0].args
    assert "source_run_history" in sql
    # status arg should be 'ok'
    assert "ok" in args


@respx.mock
async def test_fetch_new_since_writes_run_history_on_429_with_cooldown(monkeypatch):
    """A 429 response inserts a row with status='rate_limit' and calls update_last_request."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("paper_ingestion.sources.arxiv_source.asyncio.sleep", fake_sleep)
    # All 3 attempts return 429 so the source exhausts retries
    respx.get(ARXIV_API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
        ]
    )

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    source = _make_source(db_pool=mock_pool)
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML", ["machine learning"])]

    with patch(
        "paper_ingestion.sources.arxiv_source.PersistentSourceRateLimiter",
        return_value=mock_limiter,
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []

    # update_last_request was called with 'rate_limit'
    mock_limiter.update_last_request.assert_called_once()
    call_kwargs = mock_limiter.update_last_request.call_args
    assert "rate_limit" in str(call_kwargs)

    # run_history insert called with rate_limit status
    assert mock_conn.execute.called
    # LG-B3 adds a log_event call after _insert_run_history; search all calls.
    all_calls = mock_conn.execute.call_args_list
    run_history_calls = [c for c in all_calls if "source_run_history" in c.args[0]]
    assert run_history_calls, "Expected at least one execute call to source_run_history"
    sql, *args = run_history_calls[0].args
    assert "source_run_history" in sql
    assert "rate_limit" in args


# ---------------------------------------------------------------------------
# DRY-S3: pdf_url allowlist in _parse_entry
# ---------------------------------------------------------------------------


def _make_entry_xml(pdf_href: str) -> object:
    """Build a minimal Atom <entry> element with the given pdf link href."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2301.99999v1</id>
    <title>Test Paper</title>
    <summary>Abstract text.</summary>
    <published>2023-01-15T12:00:00Z</published>
    <link title="pdf" href="{pdf_href}" type="application/pdf"/>
    <link rel="alternate" href="http://arxiv.org/abs/2301.99999v1"/>
    <author><name>Test Author</name></author>
  </entry>
</feed>"""
    root = fromstring(xml)
    return root.find(f"{{{ATOM_NS}}}entry")


def test_parse_entry_rejects_non_allowlisted_pdf(caplog):
    """_parse_entry sets pdf_url=None and logs a warning for non-allowlisted hostnames."""
    import logging

    source = _make_source()
    entry = _make_entry_xml("https://evil.example.com/foo.pdf")

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.sources.arxiv_source"):
        paper = source._parse_entry(entry)

    assert paper.pdf_url is None
    assert any("evil.example.com" in r.message for r in caplog.records)


def test_parse_entry_accepts_allowlisted_pdf():
    """_parse_entry keeps pdf_url for known arxiv.org hostnames."""
    source = _make_source()
    entry = _make_entry_xml("https://arxiv.org/pdf/2301.99999v1")

    paper = source._parse_entry(entry)

    assert paper.pdf_url == "https://arxiv.org/pdf/2301.99999v1"


def test_parse_entry_rejects_http_scheme_pdf(caplog):
    """_parse_entry rejects non-http/https pdf_url schemes (e.g. ftp://)."""
    import logging

    source = _make_source()
    entry = _make_entry_xml("ftp://arxiv.org/pdf/2301.99999v1")

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.sources.arxiv_source"):
        paper = source._parse_entry(entry)

    assert paper.pdf_url is None
