"""Behavioural regression guard for the auto-fetch 3b reprocess selection (ING-1).

The 3b stage of ``run_auto_pipeline`` must select downloaded-but-unchunked
papers for processing using the ``papers.chunked_at IS NULL`` marker — NOT the
old ``NOT EXISTS (SELECT 1 FROM paper_chunks ...)`` predicate. The two diverge
exactly on a "stuck partial" paper that has chunk rows yet was never marked
complete (chunked_at still NULL): the new query MUST re-process it, the old
query would WRONGLY skip it.

This test drives the REAL ``run_auto_pipeline`` against a REAL (live_pg) pool so
the actual SQL runs, and observes which paper ids ``run_process_pdf`` is invoked
for. It asserts on SELECTION BEHAVIOUR (which ids ran), never on query text
(TS-02). It is non-tautological: P1 carries paper_chunks rows + chunked_at NULL,
so it is selected only under ``chunked_at IS NULL`` and would be dropped if the
query reverted to ``NOT EXISTS paper_chunks`` — flipping the P1 assertion red.

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
async def test_reprocess_selects_unmarked_paper_with_chunks(test_db_pool, monkeypatch):
    """3b reprocesses a paper iff chunked_at IS NULL, even if it already has chunks.

    Discriminating seed:
      P1 — downloaded, HAS a paper_chunks row, chunked_at NULL  → MUST reprocess.
           (The old ``NOT EXISTS paper_chunks`` query would skip P1 because it
           has chunks; only ``chunked_at IS NULL`` selects it. This is what makes
           the test fail on a revert rather than pass either way.)
      P2 — downloaded, HAS a paper_chunks row, chunked_at set → MUST skip.
           (Confirms the "marked complete → excluded" direction.)
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
    assert p2_id not in processed_ids, "3b must skip the completed paper (chunked_at set)"
