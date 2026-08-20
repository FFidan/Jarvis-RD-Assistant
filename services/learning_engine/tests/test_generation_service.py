"""Batch card generation must not report a truncated run as a finished deck.

One run covers a bounded number of papers. When the deck holds more than that,
the result has to carry the deck's real eligible count and the number of papers
still waiting — the browser turns those into "N not processed", and a zero
there tells the user the deck is done when it is not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from learning_engine.generation_service import _BATCH_PAPER_LIMIT, _card_generate_batch_job


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("eligible_in_run", "counted_total", "expected"),
    [
        (
            _BATCH_PAPER_LIMIT,
            _BATCH_PAPER_LIMIT + 10,
            {"total": _BATCH_PAPER_LIMIT + 10, "remaining": 10, "status": "partial", "counts": 1},
        ),
        (2, None, {"total": 2, "remaining": 0, "status": "ok", "counts": 0}),
    ],
    ids=["deck_larger_than_one_run", "deck_covered_by_one_run"],
)
async def test_batch_generation_reports_the_deck_total_and_what_is_left(
    mock_db, eligible_in_run, counted_total, expected
) -> None:
    """A run that fills its page reports the deck total; a short run needs no count."""
    pool, conn = mock_db
    conn.fetch.return_value = [{"id": paper_id} for paper_id in range(1, eligible_in_run + 1)]
    conn.fetchval.return_value = counted_total

    from tests.le_helpers import make_job_ctx

    ctx = make_job_ctx()
    with patch(
        "learning_engine.generation_service.generate_cards_core",
        AsyncMock(return_value={"cards_created": 1}),
    ):
        result = await _card_generate_batch_job(
            pool=pool,
            http_client=AsyncMock(),
            payload={"deck_id": 1, "user_id": 42},
            ctx=ctx,
        )

    assert result["papers_processed"] == eligible_in_run
    assert result["total"] == expected["total"]
    assert result["remaining"] == expected["remaining"]
    assert result["status"] == expected["status"]
    assert conn.fetchval.await_count == expected["counts"]
    terminal_message = ctx.update_progress.await_args_list[-1].args[1]
    assert f"/{expected['total']} processed" in terminal_message
