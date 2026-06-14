"""Unit test for the summary read scope in get_paper_detail.

No existing test covers that GET /api/papers/{id} binds the caller's user_id to
the paper_summaries read (the per-user workspace scope). The live-PG cross-user
test in tests/integration proves end-to-end isolation; this fast unit test
guards the query-building so a regression is caught in the default subset.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.routers import papers_detail
from tests.conftest import FakeRecord


def _paper_record(paper_id: int) -> FakeRecord:
    return FakeRecord(
        id=paper_id,
        external_id="x",
        source_type="arxiv",
        title="Shared",
        authors=["A. Author"],
        abstract=None,
        published_date=None,
        url="https://example.test/x",
        pdf_url=None,
        pdf_local_path=None,
        pdf_downloaded=False,
        citation_count=0,
        priority_score=None,
        metadata={},
        discovered_at=None,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_summary_read_binds_caller_user_id(monkeypatch):
    """The paper_summaries read must bind the caller's user_id, not just paper_id."""
    monkeypatch.setattr(papers_detail.papers_service, "assert_paper_ownership", AsyncMock())

    paper_id = 7
    user_id = 42
    pool, conn = make_pool_and_conn()
    # papers read → summary read → user_state → feedback;
    # fetch (chunks) + fetchval (link count, last job status) are stubbed empty.
    conn.fetchrow = AsyncMock(
        side_effect=[
            _paper_record(paper_id),
            None,  # summary: none for this user
            None,  # user_state
            None,  # feedback
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)

    # Call the undecorated endpoint to bypass the slowapi rate-limit wrapper,
    # which requires a real starlette Request.
    endpoint = papers_detail.get_paper_detail.__wrapped__
    request = AsyncMock()
    await endpoint(request=request, paper_id=paper_id, db_pool=pool, user_id=user_id)

    # Handler fetchrow order (per the side_effect above): papers read, summary
    # read, user_state, feedback. The summary read is the second call.
    assert conn.fetchrow.await_count == 4
    _sql, *summary_params = conn.fetchrow.call_args_list[1].args
    assert user_id in summary_params, "caller's user_id must be bound to the summary read"
