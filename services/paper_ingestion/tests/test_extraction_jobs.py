"""Unit tests for app.extraction_jobs — extraction.batch job handler.

Happy path: valid payload is processed and result counts are returned.
Edge case: missing template_id raises KeyError before batch_extract is called.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn


def _chunk_row(content: str = "A method.") -> dict:
    """One paper_chunks row carrying every column the substrate SELECT returns."""
    return {
        "id": 1,
        "paper_id": 7,
        "chunk_index": 0,
        "content": content,
        "page_number": 1,
        "start_char": None,
        "end_char": None,
        "embedding_id": None,
        "created_at": datetime.now(UTC),
    }


class _FakeCtx:
    """Minimal JobContext substitute."""

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        pass

    async def is_cancelled(self) -> bool:
        return False


def _install_fake_batch_extract(monkeypatch, result) -> None:
    """Stub app.extraction so batch_extract returns a controlled result."""
    fake_mod = types.ModuleType("paper_ingestion.extraction")
    fake_mod.batch_extract = AsyncMock(return_value=result)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paper_ingestion.extraction", fake_mod)


def _install_fake_app(monkeypatch, *, embedder=None, verifier=None) -> None:
    """Populate svc so job handlers can resolve embedder/verifier."""
    from paper_ingestion.extraction import jobs as jobs_mod  # noqa: PLC0415

    services = jobs_mod.get_services()
    services.embedder = embedder
    services.verifier = verifier


@pytest.mark.asyncio
async def test_extraction_batch_happy_path(monkeypatch):
    """Handler returns all batch outcome counts and status from batch_extract.

    The terminal status and remaining count are carried through rather than
    recomputed here, so a cancelled batch reaches the job result intact.
    """
    from paper_ingestion.extraction.jobs import _extraction_batch_job

    # Neither value can be recomputed from the other counts below, so this
    # fixture distinguishes pass-through from a locally reconstructed result.
    fake_result = SimpleNamespace(
        extracted=5,
        failed=1,
        skipped=2,
        remaining=3,
        total=13,
        status="cancelled",
    )
    _install_fake_batch_extract(monkeypatch, fake_result)
    embedder = MagicMock()
    verifier = MagicMock()
    _install_fake_app(monkeypatch, embedder=embedder, verifier=verifier)

    pool = MagicMock()
    http_client = MagicMock()
    ctx = _FakeCtx()
    payload = {"paper_ids": [10, 20, 30, 40, 50, 60, 70, 80], "template_id": 3}

    result = await _extraction_batch_job(pool, http_client, payload, ctx)

    assert result["extracted"] == 5
    assert result["failed"] == 1
    assert result["skipped"] == 2
    assert result["remaining"] == 3
    assert result["total"] == 13
    assert result["status"] == "cancelled", (
        "the job result must carry the terminal status the batch reported, "
        f"not one it decided for itself; got {result.get('status')!r}"
    )

    # batch_extract should have been called once with pool, http_client, ids, template_id
    # Use sys.modules directly: `import pkg.mod` after the real module was already loaded
    # returns the real module via package attribute lookup, bypassing the sys.modules stub.
    import sys as _sys

    extraction_mod = _sys.modules["paper_ingestion.extraction"]  # always the stub

    extraction_mod.batch_extract.assert_awaited_once()
    call_args = extraction_mod.batch_extract.await_args
    assert call_args.args[2] == [10, 20, 30, 40, 50, 60, 70, 80]
    assert call_args.args[3] == 3
    assert call_args.kwargs["embedder"] is embedder
    assert call_args.kwargs["verifier"] is verifier


@pytest.mark.asyncio
async def test_extraction_batch_missing_template_id(monkeypatch):
    """Handler raises KeyError immediately when template_id is absent from payload."""
    from paper_ingestion.extraction.jobs import _extraction_batch_job

    # batch_extract should never be called
    fake_mod = types.ModuleType("paper_ingestion.extraction")
    fake_mod.batch_extract = AsyncMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paper_ingestion.extraction", fake_mod)
    _install_fake_app(monkeypatch)

    with pytest.raises(KeyError):
        await _extraction_batch_job(MagicMock(), MagicMock(), {"paper_ids": [1, 2]}, _FakeCtx())

    fake_mod.batch_extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_extract_cancelled_mid_run_is_not_unqualified_success():
    """A batch extraction run stopped by cancellation must report a
    ``cancelled`` status and a total — never look like a clean completion."""
    from paper_ingestion.extraction.core import batch_extract

    pool, _conn = make_pool_and_conn(fetchval_return=None)

    ctx = _FakeCtx()
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(side_effect=[False, True])

    with patch(
        "paper_ingestion.extraction.core.extract_fields_for_paper",
        AsyncMock(return_value=None),
    ):
        result = await batch_extract(MagicMock(), pool, [1, 2, 3], 3, ctx=ctx)

    assert result.status == "cancelled"
    assert result.total == 3
    assert result.extracted == 1
    assert result.remaining == 2
    terminal_message = ctx.update_progress.await_args_list[-1].args[1]
    assert "Done" not in terminal_message


@pytest.mark.asyncio
async def test_batch_extract_skipped_only_is_partial():
    """An already-current extraction is skipped work, not a complete new run."""
    from paper_ingestion.extraction.core import batch_extract

    pool, _conn = make_pool_and_conn(fetchval_return=17)
    ctx = _FakeCtx()
    ctx.update_progress = AsyncMock()

    with patch(
        "paper_ingestion.extraction.core.extract_fields_for_paper",
        AsyncMock(),
    ) as extract:
        result = await batch_extract(MagicMock(), pool, [1], 3, ctx=ctx)

    assert result.extracted == 0
    assert result.failed == 0
    assert result.skipped == 1
    assert result.remaining == 0
    assert result.total == 1
    assert result.status == "partial"
    assert ctx.update_progress.await_args_list[-1].args[1].startswith("Partial:")
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_delayed_extraction_writer_returns_the_newer_generation_winner():
    """A generation-zero result cannot replace an extraction from generation one."""
    from paper_ingestion.extraction.core import extract_fields_for_paper
    from paper_ingestion.extraction.dynamic_models import ExtractedFieldOutput

    pool, conn = make_pool_and_conn()
    conn.fetchval.return_value = 1
    winner = {
        "id": 19,
        "paper_id": 7,
        "template_id": 3,
        "extractions": {"method": {"value": "generation one"}},
        "extraction_model": "newer-model",
        "content_generation": 1,
        "created_at": datetime.now(UTC),
    }
    conn.fetchrow.side_effect = [
        {
            "id": 3,
            "fields": [
                {
                    "name": "method",
                    "label": "Method",
                    "description": "method used",
                    "type": "text",
                }
            ],
        },
        {"id": 7, "title": "Paper", "content_generation": 0},
        winner,
    ]
    conn.fetch.return_value = [_chunk_row()]
    llm_result = SimpleNamespace(
        method=ExtractedFieldOutput(value="delayed generation zero"),
        model_dump_json=lambda: "{}",
    )

    with patch(
        "paper_ingestion.extraction.core.call_llm_structured",
        AsyncMock(return_value=llm_result),
    ):
        result = await extract_fields_for_paper(
            MagicMock(),
            pool,
            7,
            3,
            openai_client=MagicMock(),
            user_id=42,
        )

    assert all(
        "INSERT INTO paper_extractions" not in call.args[0]
        for call in conn.fetchrow.await_args_list
    )
    assert conn.fetchrow.await_count == 3
    assert result.content_generation == 1
    assert result.extractions["method"].value == "generation one"


@pytest.mark.asyncio
async def test_delayed_extraction_writer_without_current_winner_reports_source_change():
    """A stale completion cannot insert or report a generation-zero extraction."""
    from paper_ingestion.exceptions import SourceGenerationChangedError
    from paper_ingestion.extraction.core import extract_fields_for_paper
    from paper_ingestion.extraction.dynamic_models import ExtractedFieldOutput

    pool, conn = make_pool_and_conn()
    conn.fetchval.return_value = 1
    conn.fetchrow.side_effect = [
        {
            "id": 3,
            "fields": [
                {
                    "name": "method",
                    "label": "Method",
                    "description": "method used",
                    "type": "text",
                }
            ],
        },
        {"id": 7, "title": "Paper", "content_generation": 0},
        None,
    ]
    conn.fetch.return_value = [_chunk_row()]
    llm_result = SimpleNamespace(
        method=ExtractedFieldOutput(value="delayed generation zero"),
        model_dump_json=lambda: "{}",
    )

    with patch(
        "paper_ingestion.extraction.core.call_llm_structured",
        AsyncMock(return_value=llm_result),
    ):
        with pytest.raises(SourceGenerationChangedError, match="Please retry"):
            await extract_fields_for_paper(
                MagicMock(),
                pool,
                7,
                3,
                openai_client=MagicMock(),
                user_id=42,
            )

    assert all(
        "INSERT INTO paper_extractions" not in call.args[0]
        for call in conn.fetchrow.await_args_list
    )
    assert conn.fetchrow.await_count == 3


class _RaisingQuoteVerifier:
    """Verifier whose backend fails mid-run, leaving the quote without a verdict."""

    def verify_quote(self, *_args, **_kwargs):
        raise RuntimeError("quote verifier backend unavailable")


def _pool_echoing_persisted_extractions():
    """Pool whose ``INSERT ... RETURNING`` echoes the payload it was given.

    ``extract_fields_for_paper`` rebuilds its response from the returned row, so
    echoing is what makes the persisted field values observable to a test.
    """
    pool, conn = make_pool_and_conn()
    conn.fetchval.return_value = 0

    def _fetchrow(sql, *args):
        if "INSERT INTO paper_extractions" in sql:
            return {
                "id": 19,
                "paper_id": 7,
                "template_id": 3,
                "extractions": args[2],
                "extraction_model": "smart",
                "content_generation": 0,
                "created_at": datetime.now(UTC),
            }
        if "extraction_templates" in sql:
            return {
                "id": 3,
                "fields": [
                    {
                        "name": "accuracy",
                        "label": "Accuracy",
                        "description": "reported accuracy",
                        "type": "text",
                    }
                ],
            }
        return {"id": 7, "title": "Paper", "content_generation": 0}

    conn.fetchrow.side_effect = _fetchrow
    conn.fetch.return_value = [_chunk_row("The model reaches 94.2% accuracy on GLUE.")]
    return pool


async def _extract_one_field(verifier, quote: str):
    """Run one extraction whose single field carries *quote*, and return that field."""
    from paper_ingestion.extraction.core import extract_fields_for_paper
    from paper_ingestion.extraction.dynamic_models import ExtractedFieldOutput

    llm_result = SimpleNamespace(
        accuracy=ExtractedFieldOutput(value="95.0%", quote=quote),
        model_dump_json=lambda: "{}",
    )
    with patch(
        "paper_ingestion.extraction.core.call_llm_structured",
        AsyncMock(return_value=llm_result),
    ):
        result = await extract_fields_for_paper(
            MagicMock(),
            _pool_echoing_persisted_extractions(),
            7,
            3,
            verifier=verifier,
            openai_client=MagicMock(),
            user_id=42,
        )
    return result.extractions["accuracy"]


@pytest.mark.asyncio
async def test_rejected_quote_is_kept_so_the_reader_sees_it_labelled_unverified():
    """A quote the verifier rejected survives; only the value it failed to support is dropped."""
    from jarvis_common.verify import QuoteVerifier

    hallucinated = "This sentence appears nowhere in the paper."

    field = await _extract_one_field(QuoteVerifier(), hallucinated)

    assert field.quote == hallucinated
    assert field.value is None
    assert field.verified is False
    assert field.confidence == 0.0


@pytest.mark.asyncio
async def test_verifier_crash_drops_the_quote_because_no_verdict_can_label_it():
    """A crashed verifier leaves no verdict, so the quote cannot be surfaced at all."""
    field = await _extract_one_field(
        _RaisingQuoteVerifier(), "The model reaches 94.2% accuracy on GLUE."
    )

    assert field.quote is None
    assert field.value is None
    assert field.verified is False
    assert field.confidence == 0.0
