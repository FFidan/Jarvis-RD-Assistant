"""Tests for citation and contradiction background job handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn


def _pool_with_rows(rows: list[dict]) -> MagicMock:
    return make_pool_and_conn(fetch_return=rows, with_transaction=False)[0]


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.update_progress = AsyncMock()
    return ctx


@pytest.fixture(autouse=True)
def _reset_services():
    """Reset paper-ingestion runtime collaborators between job tests."""
    from paper_ingestion._state import reset_services

    reset_services()
    yield
    reset_services()


@pytest.mark.asyncio
async def test_citations_batch_fetch_requires_semantic_scholar_source() -> None:
    """Citation fetching should fail loudly when the S2 source is absent."""
    from paper_ingestion.citations_jobs import _citations_batch_fetch_job

    with pytest.raises(RuntimeError, match="Semantic Scholar source"):
        await _citations_batch_fetch_job(MagicMock(), MagicMock(), {}, _ctx())


@pytest.mark.asyncio
async def test_citations_batch_fetch_returns_empty_when_no_rows_need_work() -> None:
    """An empty pending-paper query should complete without progress updates."""
    from paper_ingestion._state import set_services
    from paper_ingestion.citations_jobs import _citations_batch_fetch_job

    set_services(sources={"semantic_scholar": MagicMock()})
    ctx = _ctx()

    result = await _citations_batch_fetch_job(_pool_with_rows([]), MagicMock(), {}, ctx)

    assert result == {"fetched": 0, "failed": 0, "message": "No papers need citation fetching"}
    ctx.update_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_citations_batch_fetch_counts_successes_and_failures() -> None:
    """The batch job should continue after one paper sync fails."""
    from paper_ingestion._state import set_services
    from paper_ingestion.citations_jobs import _citations_batch_fetch_job

    source = MagicMock()
    set_services(sources={"semantic_scholar": source})
    ctx = _ctx()

    with patch(
        "paper_ingestion.citations.sync_citations_for_paper",
        AsyncMock(side_effect=[None, RuntimeError("boom")]),
    ) as sync:
        result = await _citations_batch_fetch_job(
            _pool_with_rows([{"id": 10}, {"id": 20}]),
            MagicMock(),
            {},
            ctx,
        )

    assert result == {
        "fetched": 1,
        "failed": 1,
        "message": "Fetched citations for 1/2 papers",
    }
    assert sync.await_args_list[0].args[2] == 10
    assert sync.await_args_list[1].args[2] == 20
    assert [call.args[0] for call in ctx.update_progress.await_args_list] == [0.5, 1.0]


@pytest.mark.asyncio
async def test_contradictions_scan_requires_verifier_and_openai_client() -> None:
    """Contradiction scans should report missing service collaborators explicitly."""
    from paper_ingestion._state import set_services
    from paper_ingestion.contradiction_jobs import _contradictions_scan_job

    with pytest.raises(RuntimeError, match="verifier"):
        await _contradictions_scan_job(MagicMock(), MagicMock(), {}, _ctx())

    set_services(verifier=MagicMock())
    with pytest.raises(RuntimeError, match="openai_client"):
        await _contradictions_scan_job(MagicMock(), MagicMock(), {}, _ctx())


@pytest.mark.asyncio
async def test_contradictions_scan_delegates_to_service_and_reports_progress() -> None:
    """The job should pass typed payload values through to the contradiction service."""
    from paper_ingestion._state import set_services
    from paper_ingestion.contradiction_jobs import _contradictions_scan_job

    verifier = MagicMock()
    openai_client = MagicMock()
    set_services(verifier=verifier, openai_client=openai_client)
    ctx = _ctx()

    with patch(
        "paper_ingestion.contradiction_jobs.scan_contradictions",
        AsyncMock(return_value={"status": "ok"}),
    ) as scan:
        result = await _contradictions_scan_job(
            MagicMock(),
            MagicMock(),
            {"user_id": "11", "paper_id": "7", "limit": "3"},
            ctx,
        )

    assert result == {"status": "ok"}
    assert scan.await_args is not None
    assert scan.await_args.kwargs["paper_id"] == 7
    assert scan.await_args.kwargs["limit"] == 3
    assert scan.await_args.kwargs["user_id"] == 11
    assert scan.await_args.kwargs["openai_client"] is openai_client
    assert [call.args for call in ctx.update_progress.await_args_list] == [
        (0.1, "Collecting verified findings"),
        (1.0, "Done"),
    ]


@pytest.mark.asyncio
async def test_contradiction_job_extracts_user_id_from_payload() -> None:
    """user_id from the procrastinate payload must be forwarded to scan_contradictions."""
    from paper_ingestion._state import set_services
    from paper_ingestion.contradiction_jobs import _contradictions_scan_job

    set_services(verifier=MagicMock(), openai_client=MagicMock())

    with patch(
        "paper_ingestion.contradiction_jobs.scan_contradictions",
        AsyncMock(return_value={"contradictions_found": 0}),
    ) as scan:
        await _contradictions_scan_job(
            MagicMock(),
            MagicMock(),
            {"user_id": 42, "paper_id": 7, "limit": 25},
            _ctx(),
        )

    assert scan.await_args is not None
    assert scan.await_args.kwargs["user_id"] == 42, "user_id was not extracted from payload"
