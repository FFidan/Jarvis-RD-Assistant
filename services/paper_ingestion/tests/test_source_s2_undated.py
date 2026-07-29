"""Tests for M-9: fetch_new_since retains recent undated S2 papers instead of silently dropping.

S2's search API only filters at year granularity (year=YYYY-), so two classes of
papers must NOT be dropped:
  - Papers where both publicationDate and year are absent (published_date=None).
  - Papers where only year is present (published_date synthesised as Jan 1, which
    may precede since_date even within the same year).

Only papers with a *precise* publicationDate that is genuinely before since_date
should be excluded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial

import httpx
import respx
from paper_ingestion.sources.semantic_scholar_source import S2_API_URL
from tests._source_fakes import make_semantic_scholar_source, make_topic


_make_source = partial(
    make_semantic_scholar_source,
    source_id=3,
)
_make_topic = make_topic


def _s2_item(
    paper_id: str,
    *,
    year: int | None,
    publication_date: str | None,
) -> dict:
    """Build a minimal S2 paper JSON object."""
    return {
        "paperId": paper_id,
        "title": f"Paper {paper_id}",
        "authors": [{"name": "A. Author", "authorId": "99"}],
        "abstract": "An abstract.",
        "year": year,
        "publicationDate": publication_date,
        "url": f"https://www.semanticscholar.org/paper/{paper_id}",
        "citationCount": 0,
        "externalIds": {},
        "openAccessPdf": None,
        "tldr": None,
    }


# since = 2026-04-01 throughout (mid-year, so year-only papers resolve to Jan 1 < since)
_SINCE = datetime(2026, 4, 1, tzinfo=UTC)


@respx.mock
async def test_undated_paper_both_fields_absent_is_retained():
    """A paper returned by S2 with no publicationDate AND no year is kept (not silently dropped).

    S2 already bounded the query to year>=since_date.year, so the paper is
    plausibly recent enough; discarding it causes silent data loss.
    """
    item = _s2_item("undated_both", year=None, publication_date=None)
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [item]})
    )

    source = _make_source()
    papers = await source.fetch_new_since(since=_SINCE, topics=[_make_topic("ML")], limit=10)

    paper_ids = [p.external_id for p in papers]
    assert "s2:undated_both" in paper_ids, (
        "Paper with no publicationDate and no year must be retained (not silently dropped)"
    )


@respx.mock
async def test_year_only_paper_is_retained():
    """A paper with year=2026 but publicationDate=None is kept even though since_date is 2026-04-01.

    _parse_paper synthesises published_date=date(2026,1,1) which is before since_date.
    The old code would drop it at the < since_date check; the fix keeps it because
    only the year is known and S2 already filtered by year>=since_date.year.
    """
    item = _s2_item("year_only", year=2026, publication_date=None)
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [item]})
    )

    source = _make_source()
    papers = await source.fetch_new_since(since=_SINCE, topics=[_make_topic("ML")], limit=10)

    paper_ids = [p.external_id for p in papers]
    assert "s2:year_only" in paper_ids, (
        "Paper with year=2026 but no publicationDate must be retained "
        "(synthesised Jan 1 must not trigger the old-paper filter)"
    )


@respx.mock
async def test_old_dated_paper_is_dropped():
    """A paper with a precise publicationDate clearly before since_date is excluded."""
    item = _s2_item("old_precise", year=2026, publication_date="2026-01-15")
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [item]})
    )

    source = _make_source()
    papers = await source.fetch_new_since(since=_SINCE, topics=[_make_topic("ML")], limit=10)

    paper_ids = [p.external_id for p in papers]
    assert "s2:old_precise" not in paper_ids, (
        "Paper with publicationDate='2026-01-15' (before since_date 2026-04-01) must be dropped"
    )


@respx.mock
async def test_recent_dated_paper_is_retained():
    """A paper with a precise publicationDate on or after since_date is included."""
    item = _s2_item("recent_precise", year=2026, publication_date="2026-04-15")
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [item]})
    )

    source = _make_source()
    papers = await source.fetch_new_since(since=_SINCE, topics=[_make_topic("ML")], limit=10)

    paper_ids = [p.external_id for p in papers]
    assert "s2:recent_precise" in paper_ids, (
        "Paper with publicationDate='2026-04-15' (on or after since_date) must be retained"
    )


@respx.mock
async def test_mixed_batch_filters_correctly():
    """fetch_new_since correctly partitions a mixed batch of S2 papers."""
    items = [
        _s2_item("keep_undated", year=None, publication_date=None),
        _s2_item("keep_year_only", year=2026, publication_date=None),
        _s2_item("keep_recent", year=2026, publication_date="2026-05-01"),
        _s2_item("drop_old", year=2026, publication_date="2026-02-20"),
    ]
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": items})
    )

    source = _make_source()
    papers = await source.fetch_new_since(since=_SINCE, topics=[_make_topic("ML")], limit=10)

    paper_ids = {p.external_id for p in papers}
    assert "s2:keep_undated" in paper_ids
    assert "s2:keep_year_only" in paper_ids
    assert "s2:keep_recent" in paper_ids
    assert "s2:drop_old" not in paper_ids, "Precisely-dated old paper must still be excluded"
