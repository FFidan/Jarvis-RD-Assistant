"""Citation freshness: only a fetch that reached Semantic Scholar counts as fresh.

``citations_fetched_at`` gates the staleness sweep for a full refresh interval,
so stamping it after a failed provider call silences the paper for a month and
leaves the user with a citation graph that never fills in.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common.testing import FakeRecord, make_pool_and_conn

from paper_ingestion.citations import sync_citations_for_paper

_FRESHNESS_STAMP = "citations_fetched_at"


def _stamp_calls(conn) -> list:
    """Return the executed statements that claim citation freshness."""
    return [call for call in conn.execute.await_args_list if _FRESHNESS_STAMP in call.args[0]]


def _s2_source(*, citations, references) -> AsyncMock:
    """Return a Semantic Scholar client whose two directions behave as given.

    An exception value makes that direction raise; a list makes it return.
    """
    source = AsyncMock()
    for name, outcome in (("fetch_citations", citations), ("fetch_references", references)):
        call = (
            AsyncMock(side_effect=outcome)
            if isinstance(outcome, Exception)
            else AsyncMock(return_value=outcome)
        )
        setattr(source, name, call)
    return source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("citations", "references"),
    [
        (RuntimeError("citations unavailable"), []),
        ([], RuntimeError("references unavailable")),
    ],
    ids=["citations_direction_failed", "references_direction_failed"],
)
async def test_a_failed_citation_fetch_does_not_claim_freshness(citations, references) -> None:
    """A provider failure in either direction must leave the freshness stamp alone."""
    _pool, conn = make_pool_and_conn(
        fetchrow_return=FakeRecord({"external_id": "s2:abc123", "metadata": {}})
    )

    result = await sync_citations_for_paper(
        conn, _s2_source(citations=citations, references=references), 42
    )

    assert result.citations_added == 0
    assert result.references_added == 0
    assert _stamp_calls(conn) == [], "a failed fetch must not hide the paper from the sweep"


@pytest.mark.asyncio
async def test_a_complete_citation_fetch_claims_freshness() -> None:
    """Both directions reaching the provider is what starts the refresh interval."""
    _pool, conn = make_pool_and_conn(
        fetchrow_return=FakeRecord({"external_id": "s2:abc123", "metadata": {}})
    )

    await sync_citations_for_paper(conn, _s2_source(citations=[], references=[]), 42)

    assert len(_stamp_calls(conn)) == 1


@pytest.mark.asyncio
async def test_a_paper_without_a_provider_identifier_claims_nothing() -> None:
    """A locally uploaded paper is never fetched, so it is never marked fresh."""
    _pool, conn = make_pool_and_conn(
        fetchrow_return=FakeRecord({"external_id": "local:7", "metadata": {}})
    )
    source = _s2_source(citations=[], references=[])

    result = await sync_citations_for_paper(conn, source, 42)

    assert result.stubs_created == 0
    assert source.fetch_citations.await_count == 0
    assert _stamp_calls(conn) == []
