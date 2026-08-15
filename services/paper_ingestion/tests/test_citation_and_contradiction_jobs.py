"""Tests for citation and contradiction background job handlers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def _citation_source() -> MagicMock:
    source = MagicMock()
    source.fetch_citations = AsyncMock(return_value=[])
    source.fetch_references = AsyncMock(return_value=[])
    return source


@pytest.mark.asyncio
async def test_citation_sync_translates_doi_identifier_for_semantic_scholar() -> None:
    """The citation client must receive S2's DOI form, not the stored prefix."""
    from paper_ingestion.citations import sync_citations_for_paper

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "external_id": "doi:10.1000/example",
        "metadata": {},
    }
    source = _citation_source()

    await sync_citations_for_paper(conn, source, 7)

    source.fetch_citations.assert_awaited_once_with("DOI:10.1000/example")
    source.fetch_references.assert_awaited_once_with("DOI:10.1000/example")


@pytest.mark.asyncio
async def test_citation_sync_prefers_semantic_scholar_metadata_identifier() -> None:
    """A canonical S2 identifier remains authoritative over a stored DOI."""
    from paper_ingestion.citations import sync_citations_for_paper

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "external_id": "doi:10.1000/example",
        "metadata": {"s2_id": "canonical-s2-id"},
    }
    source = _citation_source()

    await sync_citations_for_paper(conn, source, 7)

    source.fetch_citations.assert_awaited_once_with("canonical-s2-id")
    source.fetch_references.assert_awaited_once_with("canonical-s2-id")


@pytest.mark.asyncio
async def test_citation_sync_skips_local_uploads() -> None:
    """A local upload has no external scholarly identifier to send to S2."""
    from paper_ingestion.citations import sync_citations_for_paper

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "external_id": "local:uploaded-paper.pdf",
        "metadata": {"s2_id": "must-not-be-used"},
    }
    source = _citation_source()

    result = await sync_citations_for_paper(conn, source, 7)

    assert result.citations_added == 0
    assert result.references_added == 0
    source.fetch_citations.assert_not_awaited()
    source.fetch_references.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected_identifier"),
    [
        ({"doi": "10.1000/openalex"}, "DOI:10.1000/openalex"),
        ({}, "URL:https://openalex.org/W123456789"),
    ],
)
async def test_citation_sync_translates_openalex_identifier(
    metadata: dict, expected_identifier: str
) -> None:
    """OpenAlex papers use a DOI when known and their canonical URL otherwise."""
    from paper_ingestion.citations import sync_citations_for_paper

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "external_id": "openalex:W123456789",
        "metadata": metadata,
    }
    source = _citation_source()

    await sync_citations_for_paper(conn, source, 7)

    source.fetch_citations.assert_awaited_once_with(expected_identifier)
    source.fetch_references.assert_awaited_once_with(expected_identifier)


@pytest.mark.asyncio
async def test_citation_sync_failure_does_not_modify_stored_edges() -> None:
    """Provider failures perform no edge writes or deletes."""
    from paper_ingestion.citations import sync_citations_for_paper

    conn = AsyncMock()
    conn.fetchrow.return_value = {"external_id": "s2:paper-7", "metadata": {}}
    source = _citation_source()
    source.fetch_citations.side_effect = RuntimeError("unavailable")
    source.fetch_references.side_effect = RuntimeError("unavailable")

    await sync_citations_for_paper(conn, source, 7)

    conn.fetchval.assert_not_awaited()
    # The single write is this paper's fetch-time stamp, bound by value — no
    # edge was inserted and none was deleted.
    assert conn.execute.await_count == 1
    stamped_at, stamped_paper_id = conn.execute.await_args.args[1:]
    assert isinstance(stamped_at, datetime)
    assert stamped_paper_id == 7


@pytest.mark.asyncio
async def test_citation_refresh_uses_thirty_day_staleness_window() -> None:
    """Fresh graph data is reused while older graph data is synchronized."""
    from paper_ingestion.citations import _refresh_stale_citations

    now = datetime(2026, 8, 15, tzinfo=UTC)
    pool, conn = make_pool_and_conn(with_transaction=False)
    conn.fetch.return_value = [
        {"id": 10, "citations_fetched_at": now - timedelta(days=29)},
        {"id": 20, "citations_fetched_at": now - timedelta(days=31)},
    ]

    with patch(
        "paper_ingestion.citations.sync_citations_for_paper",
        AsyncMock(),
    ) as sync:
        await _refresh_stale_citations(pool, _citation_source(), [10, 20], now=now)

    sync.assert_awaited_once()
    assert sync.await_args.args[2] == 20


@pytest.mark.asyncio
async def test_citation_refresh_failure_returns_without_graph_mutation() -> None:
    """A refresh error is contained before the stored graph is read."""
    from paper_ingestion.citations import _refresh_stale_citations

    now = datetime(2026, 8, 15, tzinfo=UTC)
    pool, conn = make_pool_and_conn(with_transaction=False)
    conn.fetch.return_value = [{"id": 20, "citations_fetched_at": now - timedelta(days=31)}]

    with patch(
        "paper_ingestion.citations.sync_citations_for_paper",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    ):
        await _refresh_stale_citations(pool, _citation_source(), [20], now=now)

    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


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
