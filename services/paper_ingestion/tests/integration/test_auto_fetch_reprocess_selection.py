"""Behavioural guard for bounded auto-fetch embedding reconciliation.

The processing stage prioritizes an unfinished paper even when partial chunk
rows already exist. It also includes a bounded completed-paper candidate so the
workflow can detect vectors lost from Qdrant. This test runs the shipped query
against PostgreSQL and observes the paper IDs passed to ``run_process_pdf``.

Run (Docker PostgreSQL required)::

    JARVIS_RUN_LIVE_PG=1 uv run pytest -c pyproject.toml \
        services/paper_ingestion/tests/integration/test_auto_fetch_reprocess_selection.py -q
"""

from __future__ import annotations

from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from paper_ingestion.pipelines import auto_fetch as af

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_pg,
]


async def _seed_paper(conn, *, external_id: str, chunked_at, with_chunk: bool) -> int:
    """Insert a downloaded paper under PDF storage; optionally a chunk row.

    ``chunked_at`` is a ``datetime`` (paper marked complete) or ``None``
    (not yet completed). ``with_chunk`` controls whether a paper_chunks row
    exists for it.
    """
    paper_id = await conn.fetchval(
        """INSERT INTO papers
               (external_id, source_type, title, authors, url,
                pdf_downloaded, pdf_local_path, chunked_at)
           VALUES ($1, 'arxiv', $2, ARRAY['A. Author'],
                   $3, TRUE, $4, $5)
           RETURNING id""",
        external_id,
        f"title-{external_id}",
        f"https://example.test/{external_id}",
        f"{af.PDF_STORAGE_PATH}/{external_id}.pdf",
        chunked_at,
    )
    if with_chunk:
        await conn.execute(
            "INSERT INTO paper_chunks (paper_id, chunk_index, content) VALUES ($1, 0, 'x')",
            paper_id,
        )
    return paper_id


@pytest.mark.asyncio
async def test_reprocess_selects_unmarked_and_completed_probe_candidates(test_db_pool, monkeypatch):
    """Incomplete work and a bounded completed candidate both reach the workflow.

    Discriminating seed:
      P1 — downloaded, partial chunks, chunked_at NULL: finish processing.
      P2 — downloaded, chunks complete, chunked_at set: probe vector health.
    """
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    completed_at = datetime.now(UTC)
    async with test_db_pool.acquire() as conn:
        p1_id = await _seed_paper(
            conn, external_id="reproc-stuck-partial", chunked_at=None, with_chunk=True
        )
        p2_id = await _seed_paper(
            conn,
            external_id="reproc-complete",
            chunked_at=completed_at,
            with_chunk=True,
        )

    spy = AsyncMock()
    monkeypatch.setattr(af, "run_process_pdf", spy)

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=test_db_pool,
            http_client=MagicMock(),
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )
    )

    await af.run_auto_pipeline(app)

    # First positional arg to run_process_pdf is the paper_id.
    processed_ids = {call.args[0] for call in spy.await_args_list}
    assert p1_id in processed_ids, (
        "3b must reprocess the stuck-partial paper (chunked_at IS NULL) even "
        "though it already has paper_chunks rows; a NOT EXISTS-chunks query "
        "would wrongly skip it"
    )
    assert p2_id in processed_ids, "3b must probe bounded completed papers for lost vectors"
