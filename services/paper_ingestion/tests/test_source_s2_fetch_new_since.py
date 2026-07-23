"""Tests for ``SemanticScholarSource.fetch_new_since``.

Covers topic-query construction, rate-limit diagnostics, and restore-quarantine
propagation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from jarvis_common.maintenance import OutboundEgressBlockedError
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.semantic_scholar_source import S2_API_URL, SemanticScholarSource


def _make_source() -> SemanticScholarSource:
    config = PaperSourceConfig(
        id=2,
        source_type=SourceType.SEMANTIC_SCHOLAR,
        enabled=True,
        config={},
    )
    client = httpx.AsyncClient()
    return SemanticScholarSource(config, client)


async def test_fetch_new_since_propagates_outbound_quarantine(monkeypatch):
    """A quarantine race remains retryable instead of looking like no data."""
    source = _make_source()

    async def blocked_fetch(*_args, **_kwargs):
        raise OutboundEgressBlockedError("outbound work is quarantined")

    monkeypatch.setattr(source, "_fetch_json", blocked_fetch)

    with pytest.raises(OutboundEgressBlockedError, match="quarantined"):
        await source.fetch_new_since(
            since=datetime(2026, 5, 1, tzinfo=UTC),
            topics=[TopicRef(id=1, name="GNN", query_terms=["GNN"])],
            limit=10,
        )


@respx.mock
async def test_fetch_new_since_searches_topics_and_filters_recent_publications():
    """fetch_new_since passes joined query_terms as 'query' and year= filter to S2.

    Grounds: fetch_new_since builds query = ' OR '.join(query_terms) (line ~344)
    and params['year'] = f'{since_date.year}-' (line ~372).  Papers with a
    precise publicationDate before since_date are dropped client-side.
    """
    route = respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "paperId": "p1",
                        "title": "New Neural CDE Paper",
                        "authors": [{"name": "A", "authorId": "1"}],
                        "abstract": "Recent work",
                        "year": 2026,
                        "publicationDate": "2026-05-02",
                        "url": "https://www.semanticscholar.org/paper/p1",
                        "citationCount": 5,
                        "externalIds": {"ArXiv": "2605.00001"},
                    },
                    {
                        "paperId": "old",
                        "title": "Old Paper",
                        "authors": [{"name": "B"}],
                        "abstract": "Old work",
                        "year": 2025,
                        "publicationDate": "2025-01-01",
                        "url": "https://www.semanticscholar.org/paper/old",
                        "citationCount": 1,
                        "externalIds": {},
                    },
                ]
            },
        )
    )

    source = _make_source()
    papers = await source.fetch_new_since(
        since=datetime(2026, 5, 1, tzinfo=UTC),
        topics=[TopicRef(id=1, name="Neural CDE", query_terms=["Neural CDE", "NCDE"])],
        limit=10,
    )

    assert route.call_count == 1
    params = dict(route.calls[0].request.url.params)
    # query_terms joined with OR — live construction in fetch_new_since
    assert params["query"] == "Neural CDE OR NCDE"
    # year-granularity filter built from since.year
    assert params["year"] == "2026-"
    # old paper (precise publicationDate 2025-01-01 < since 2026-05-01) is dropped
    assert [paper.external_id for paper in papers] == ["s2:p1"]


@respx.mock
async def test_fetch_new_since_429_records_rate_limit_diagnostic():
    """fetch_new_since returns [] on HTTP 429 and records a rate_limit diagnostic.

    Grounds: _fetch_json calls _record_transient_poll_diagnostic on 429, which
    sets last_poll_diagnostic['status']='rate_limit', status_code=429, and
    retry_after_s=int(Retry-After header).  fetch_new_since returns [] when
    _fetch_json returns {} (empty dict on 429).
    """
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "23"})
    )

    source = _make_source()
    papers = await source.fetch_new_since(
        since=datetime(2026, 5, 1, tzinfo=UTC),
        topics=[TopicRef(id=1, name="graph neural networks", query_terms=["GNN"])],
        limit=10,
    )

    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] == "rate_limit"
    assert source.last_poll_diagnostic["status_code"] == 429
    assert source.last_poll_diagnostic["retry_after_s"] == 23
