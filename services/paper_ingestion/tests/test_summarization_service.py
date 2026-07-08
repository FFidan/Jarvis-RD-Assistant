"""Unit tests for the summarization service module."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jarvis_common.verify import QuoteVerifier

from paper_ingestion.exceptions import EmptyChunksError, LLMError, PaperNotFoundError
from paper_ingestion.models import Confidence
from paper_ingestion.services import summarization
from paper_ingestion.services.summarization_models import (
    CondensedDigest,
    KeyFindingOutput,
    ReduceSummary,
    SummarizationOutput,
    WindowDigest,
)


@asynccontextmanager
async def _noop_lock(*args, **kwargs):
    yield


# Keep local: multi-acquire side_effect semantics (successive acquire() yields different conns) not covered by canonical make_pool_and_conn.
def _make_pool(*connections: AsyncMock) -> MagicMock:
    """Create a pool mock that yields the provided connections in order."""
    contexts = []
    for conn in connections:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        contexts.append(ctx)

    pool = MagicMock()
    pool.acquire.side_effect = contexts
    return pool


def _paper_row() -> dict:
    """Return a minimal paper row for summary tests."""
    return {
        "id": 7,
        "title": "Test Paper",
        "authors": ["Ada"],
        "abstract": "Original abstract text.",
        "metadata": {"s2_tldr": "semantic scholar summary"},
    }


def _chunk_row() -> dict:
    """Return a minimal chunk DB row."""
    return {
        "id": 1,
        "paper_id": 7,
        "chunk_index": 0,
        "content": "This paper improves retrieval quality.",
        "page_number": 2,
        "start_char": 0,
        "end_char": 35,
        "embedding_id": "vec-1",
        "created_at": datetime.now(UTC),
    }


def _patched_call_llm(return_value=None, side_effect=None):
    """Patch summarization.call_llm_structured with a controllable AsyncMock."""
    mock = AsyncMock(return_value=return_value, side_effect=side_effect)
    return patch.object(summarization, "call_llm_structured", mock), mock


@pytest.fixture(autouse=True)
def _stub_openai_client(monkeypatch):
    """Avoid `svc.openai_client is None` failures inside generate_paper_summary."""
    monkeypatch.setattr(summarization.svc, "openai_client", MagicMock())


@pytest.mark.asyncio
async def test_find_cross_references_prefers_semantic_results():
    """Semantic search results are deduplicated and preferred over keyword fallback."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": "Abstract", "discovered_by": None}
    embedder = AsyncMock()
    embedder.search_similar.return_value = [
        {"paper_id": 2, "score": 0.91},
        {"paper_id": 2, "score": 0.85},
        {"paper_id": 3, "score": 0.75},
    ]

    result = await summarization._find_cross_references(conn, 7, "Test Paper", embedder=embedder)

    assert [item.related_paper_id for item in result] == [2, 3]
    assert all(item.relationship == "semantic_similarity" for item in result)
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_find_cross_references_returns_empty_when_semantic_unavailable():
    """Semantic search failure yields [] — there is no keyword fallback anymore."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": "Abstract", "discovered_by": None}
    embedder = AsyncMock()
    embedder.search_similar.side_effect = RuntimeError("qdrant down")

    result = await summarization._find_cross_references(
        conn,
        7,
        "Retrieval Agents Systems",
        embedder=embedder,
    )

    assert result == []
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_generate_paper_summary_returns_existing_summary():
    """Existing summaries short-circuit before any LLM call."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), {"id": 1}]
    pool = _make_pool(conn)
    verifier = MagicMock()
    embedder = MagicMock()
    http_client = AsyncMock()

    patch_ctx, llm_mock = _patched_call_llm()
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(
            summarization, "row_to_summary_response", return_value="existing-summary"
        ) as convert,
        patch_ctx,
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=http_client,
            verifier=verifier,
            embedder=embedder,
        )

    assert result.summary == "existing-summary"
    assert result.passes == 0
    llm_mock.assert_not_called()
    convert.assert_called_once()


@pytest.mark.asyncio
async def test_generate_paper_summary_idempotency_scoped_by_user_id():
    """The idempotency lookup must be scoped by user_id.

    paper_summaries is per-user (UNIQUE (paper_id, user_id)); an unscoped
    'WHERE paper_id = $1' check would return ANOTHER user's summary content to
    the caller. The check must bind the caller's user_id.
    """
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), {"id": 1}]  # paper found, existing summary
    pool = _make_pool(conn)

    patch_ctx, _llm_mock = _patched_call_llm()
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "row_to_summary_response", return_value="existing"),
        patch_ctx,
    ):
        await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=MagicMock(),
            embedder=MagicMock(),
            user_id=42,
        )

    # fetchrow[0] = paper-existence check; fetchrow[1] = the idempotency lookup.
    idempotency_call = conn.fetchrow.call_args_list[1]
    assert 42 in idempotency_call.args, (
        "idempotency check must bind the caller's user_id so the query scopes "
        f"per-user; call args were {idempotency_call.args!r}"
    )


@pytest.mark.asyncio
async def test_generate_paper_summary_raises_on_missing_paper():
    """A missing paper must raise PaperNotFoundError (not HTTPException) before any LLM call."""
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    pool = _make_pool(conn)
    http_client = AsyncMock()

    patch_ctx, llm_mock = _patched_call_llm()
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch_ctx,
    ):
        with pytest.raises(PaperNotFoundError, match="7"):
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_generate_paper_summary_raises_on_missing_chunks():
    """Papers without processed chunks must raise EmptyChunksError (not HTTPException)."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = []
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)
    http_client = AsyncMock()

    patch_ctx, llm_mock = _patched_call_llm()
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch_ctx,
    ):
        with pytest.raises(EmptyChunksError, match="process-pdf"):
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_generate_paper_summary_maps_read_timeout_to_llm_error():
    """ReadTimeout from LiteLLM must raise LLMError (not HTTPException)."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)
    http_client = AsyncMock()

    patch_ctx, _llm_mock = _patched_call_llm(side_effect=httpx.ReadTimeout("slow"))
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch_ctx,
    ):
        with pytest.raises(LLMError, match="timed out"):
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )


# ---------------------------------------------------------------------------
# F4: LLM error cause preservation + no body leak + bounded transient retry
# ---------------------------------------------------------------------------


def _http_status_error(status_code: int, body: str) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError whose response carries a sentinel body."""
    request = httpx.Request("POST", "http://litellm/v1/chat/completions")
    response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError("upstream error", request=request, response=response)


async def _drive_summary_llm_failure(side_effect):
    """Drive generate_paper_summary down the single-window LLM path with a failing call.

    Returns (raised LLMError, the patched call_llm_structured mock).
    """
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)

    patch_ctx, llm_mock = _patched_call_llm(side_effect=side_effect)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_RETRY_BACKOFF_SECONDS", 0.0),
        patch_ctx,
    ):
        with pytest.raises(LLMError) as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=AsyncMock(),
                verifier=MagicMock(),
                embedder=MagicMock(),
            )
    return exc_info.value, llm_mock


@pytest.mark.asyncio
async def test_llm_error_carries_status_code_but_not_response_body(caplog):
    """An HTTPStatusError surfaces only the status code; the upstream body never leaks.

    LLMError subclasses JobError, whose str(exc) is rendered VERBATIM to the user
    via the job-status 'error' field (task_registry._terminal_error_payload). The
    user-facing message must therefore carry the HTTP status code ONLY — never the
    upstream response body — while the full cause goes to logger.error(exc_info=True).
    """
    import logging as _logging

    err = _http_status_error(502, "SECRET_BODY_LEAK from the upstream provider")
    with caplog.at_level(_logging.ERROR, logger="paper_ingestion.services.summarization"):
        raised, _llm_mock = await _drive_summary_llm_failure(err)

    # User-facing message: status code present, body sentinel absent.
    assert "502" in str(raised)
    assert "SECRET_BODY_LEAK" not in str(raised)

    # Server-side: the failure is logged with exc_info and correlates the paper.
    error_records = [r for r in caplog.records if r.levelno == _logging.ERROR]
    assert error_records, "a logger.error must be emitted for the failed LLM call"
    assert any(r.exc_info is not None for r in error_records), "logged with exc_info"
    assert any("7" in str(r.getMessage()) or 7 in (r.args or ()) for r in error_records), (
        "the failed-call log must correlate the paper_id"
    )


@pytest.mark.asyncio
async def test_api_status_error_does_not_leak_response_body():
    """openai.APIStatusError carries the full upstream body in str(exc) — never surface it."""
    import openai

    request = httpx.Request("POST", "http://litellm/v1/chat/completions")
    response = httpx.Response(500, request=request, text="SECRET_BODY_LEAK")
    api_err = openai.APIStatusError("upstream 500: SECRET_BODY_LEAK", response=response, body=None)
    raised, _llm_mock = await _drive_summary_llm_failure(api_err)

    assert "SECRET_BODY_LEAK" not in str(raised)
    assert "500" in str(raised)


@pytest.mark.asyncio
async def test_permanent_error_raises_after_a_single_attempt():
    """A permanent error (4xx HTTPStatusError) must NOT retry — exactly one LLM call."""
    err = _http_status_error(400, "bad request")
    _raised, llm_mock = await _drive_summary_llm_failure(err)
    assert llm_mock.call_count == 1, "permanent errors must raise on the first attempt"


@pytest.mark.asyncio
async def test_transient_5xx_retries_at_most_twice():
    """A transient 5xx that never recovers retries up to the cap, then raises LLMError."""
    err = _http_status_error(503, "upstream unavailable")
    _raised, llm_mock = await _drive_summary_llm_failure(err)
    assert llm_mock.call_count == 2, "transient errors retry at most once (2 attempts total)"


@pytest.mark.asyncio
async def test_transient_then_success_returns_summary():
    """A transient 502 on the first attempt recovers on the retry and produces a summary."""
    llm_output = SummarizationOutput(
        tldr="A good paper",
        summary_brief="Brief summary",
        summary_detailed="Detailed summary",
        key_findings=[],
    )
    transient = _http_status_error(502, "transient")

    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = [_chunk_row()]
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = _stored_row()
    pool = _make_pool(conn_phase1, conn_phase2)

    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=1, verified_count=1, confidence=Confidence.HIGH
    )

    patch_ctx, llm_mock = _patched_call_llm(side_effect=[transient, llm_output])
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch.object(summarization, "_RETRY_BACKOFF_SECONDS", 0.0),
        patch_ctx,
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=verifier,
            embedder=MagicMock(),
        )

    assert llm_mock.call_count == 2
    assert result.summary.summary_brief == "brief"


@pytest.mark.asyncio
async def test_generate_paper_summary_confidence_none_roundtrips_without_validation_error():
    """Confidence.NONE (zero findings) must survive the DB round-trip without ValidationError.

    Regression guard: jarvis_common.verify.Confidence.NONE was not in the local
    Confidence enum, so row_to_summary_response raised a Pydantic ValidationError on
    read-back whenever verify_findings returned NONE (empty findings list).

    Also pins the degrade contract: the LLM's own brief/detailed survive
    verification failure (abstract substitution happens ONLY when the parsed
    text is empty), and the NONE→LOW confidence mapping for the DB CHECK
    constraint is unchanged.
    """
    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = [_chunk_row()]
    conn_phase1.fetchval.return_value = "smart"

    stored_row = {
        "id": 5,
        "paper_id": 7,
        "summary_brief": "draft brief",
        "summary_detailed": "draft detailed",
        "tldr": "semantic scholar summary",
        "key_findings": [],
        "methodology": None,
        "limitations": None,
        "relevance_notes": None,
        "confidence": "NONE",
        "cross_references": [],
        "llm_model": "smart-model",
        "summary_verified": False,
        "created_at": datetime.now(UTC),
    }
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = stored_row
    pool = _make_pool(conn_phase1, conn_phase2)

    llm_output = SummarizationOutput(
        tldr="",
        summary_brief="draft brief",
        summary_detailed="draft detailed",
        key_findings=[],
    )

    http_client = AsyncMock()
    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=0,
        verified_count=0,
        confidence=Confidence.NONE,
    )

    patch_ctx, _llm_mock = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        # Must not raise ValidationError — the core regression assertion
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=http_client,
            verifier=verifier,
            embedder=MagicMock(),
        )

    assert result.summary.confidence == Confidence.NONE
    assert result.summary.summary_verified is False

    # Degrade contract: parsed brief/detailed are kept verbatim ($2/$3) and
    # confidence NONE is mapped to LOW ($9) for the DB CHECK constraint.
    insert_args = conn_phase2.fetchrow.call_args.args
    assert insert_args[2] == "draft brief"
    assert insert_args[3] == "draft detailed"
    assert insert_args[9] == "LOW"


# ---------------------------------------------------------------------------
# semantic-only cross-reference contract (keyword fallback deleted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_references_filter_unseen_papers():
    """No embedder → [] with zero DB queries.

    The keyword ILIKE fallback (which linked unrelated papers on any shared
    >3-char title word) is deleted; without a semantic path there is nothing
    to return and no query that could leak other users' papers.
    """
    conn = AsyncMock()

    result = await summarization._find_cross_references(
        conn,
        paper_id=7,
        title="Retrieval Augmented Generation Systems",
        embedder=None,
    )

    assert result == []
    conn.fetchrow.assert_not_called()
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_cross_references_semantic_path_scopes_to_requester_not_owner():
    """The cross-ref search must bind the REQUESTING user, not the paper owner.

    Inverts the prior owner-scoping pin: binding the owner (``discovered_by``)
    let one tenant's cross-ref pull in another tenant's chunks.  The requester's
    own ``user_library`` is threaded as ``library_paper_ids`` (PI-RAG-001) so
    shared-corpus papers in the requester's library stay reachable — verified
    by the positive assertion below.
    """
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": None, "discovered_by": 99}
    # The requester's own library: a shared-corpus paper embedded by another user.
    conn.fetch.return_value = [{"paper_id": 7}, {"paper_id": 555}]
    embedder = AsyncMock()
    embedder.search_similar.return_value = []

    result = await summarization._find_cross_references(
        conn,
        paper_id=7,
        title="Retrieval Augmented Generation",
        embedder=embedder,
        requester_id=42,
    )

    assert result == []
    kwargs = embedder.search_similar.call_args.kwargs
    assert kwargs["user_id"] == 42, "search must scope to the requester (42), not the owner (99)"
    assert kwargs["library_paper_ids"] == [7, 555], (
        "the requester's own user_library must be threaded so a shared-corpus "
        "paper (555) embedded by another user stays reachable (PI-RAG-001)"
    )
    # The library lookup must be keyed on the REQUESTER's id.
    assert conn.fetch.await_args.args[-1] == 42


@pytest.mark.asyncio
async def test_cross_references_semantic_path_falls_back_to_owner_for_system_jobs():
    """With no requester (system job), the search falls back to the paper owner."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": None, "discovered_by": 99}
    conn.fetch.return_value = [{"paper_id": 7}]
    embedder = AsyncMock()
    embedder.search_similar.return_value = []

    result = await summarization._find_cross_references(
        conn,
        paper_id=7,
        title="Retrieval Augmented Generation",
        embedder=embedder,
    )

    assert result == []
    assert embedder.search_similar.call_args.kwargs["user_id"] == 99
    assert embedder.search_similar.call_args.kwargs["library_paper_ids"] == [7]
    assert conn.fetch.await_args.args[-1] == 99


@pytest.mark.asyncio
async def test_cross_references_db_filters_qdrant_hits_against_current_library():
    """Qdrant hits must pass a final relational user-library visibility check."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": None, "discovered_by": 99}
    conn.fetch.side_effect = [
        [{"paper_id": 7}, {"paper_id": 555}],
        [{"paper_id": 555}],
    ]
    embedder = AsyncMock()
    embedder.search_similar.return_value = [
        {"paper_id": 555, "score": 0.9},
        {"paper_id": 999, "score": 0.95},
    ]

    result = await summarization._find_cross_references(
        conn,
        paper_id=7,
        title="Retrieval Augmented Generation",
        embedder=embedder,
        requester_id=42,
    )

    assert [r.related_paper_id for r in result] == [555]
    assert conn.fetch.await_args_list[1].args == (
        "SELECT paper_id FROM user_library WHERE user_id = $1 AND paper_id = ANY($2::int[])",
        42,
        [999, 555],
    )


# ---------------------------------------------------------------------------
# INSERT includes user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_paper_summary_persists_user_id():
    """INSERT into paper_summaries must include user_id column.

    Two users summarizing the same paper previously both upserted the NULL-user
    row, causing mutual overwrite.  The fix adds user_id to the column list and
    binds the caller-supplied value.
    """
    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]  # paper found, no existing summary
    conn_phase1.fetch.return_value = [_chunk_row()]

    stored_row = {
        "id": 5,
        "paper_id": 7,
        "user_id": 42,
        "summary_brief": "brief text",
        "summary_detailed": "detailed text",
        "tldr": "tldr text",
        "key_findings": [],
        "methodology": None,
        "limitations": None,
        "relevance_notes": None,
        "confidence": "HIGH",
        "cross_references": [],
        "llm_model": "smart",
        "summary_verified": True,
        "created_at": datetime.now(UTC),
    }
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = stored_row
    pool = _make_pool(conn_phase1, conn_phase2)

    llm_output = SummarizationOutput(
        tldr="A good paper",
        summary_brief="Brief summary",
        summary_detailed="Detailed summary",
        key_findings=[],
    )

    http_client = AsyncMock()
    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=1,
        verified_count=1,
        confidence=Confidence.HIGH,
    )

    patch_ctx, _llm_mock = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=http_client,
            verifier=verifier,
            embedder=MagicMock(),
            user_id=42,
        )

    # Behaviour-shape assertion: user_id reaches the INSERT as a bind parameter.
    # Column-list shape is exercised by the live-PG contract test, not here.
    assert conn_phase2.fetchrow.called, "phase-2 connection should have issued INSERT"
    insert_call = conn_phase2.fetchrow.call_args
    bound_params = insert_call.args[1:]
    assert 42 in bound_params, f"user_id=42 should be bound as a parameter; got: {bound_params}"


# ---------------------------------------------------------------------------
# ValidationError → LLMError (not HTTPException)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_paper_summary_raises_llm_error_on_pydantic_validation_error():
    """pydantic.ValidationError from LLM parsing must raise LLMError, not HTTPException.

    Regression coverage: call_llm_structured raising a
    pydantic.ValidationError is caught and re-raised as LLMError("Malformed LLM response").
    """
    import pydantic

    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    pool = _make_pool(conn)

    patch_ctx, _llm_mock = _patched_call_llm(
        side_effect=pydantic.ValidationError.from_exception_data(
            title="SummarizationOutput",
            input_type="python",
            line_errors=[
                {
                    "type": "missing",
                    "loc": ("summary_brief",),
                    "msg": "Field required",
                    "input": {},
                    "url": "https://errors.pydantic.dev/2/v/missing",
                }
            ],
        )
    )
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch_ctx,
    ):
        with pytest.raises(LLMError, match="Malformed LLM response"):
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=AsyncMock(),
                verifier=MagicMock(),
                embedder=MagicMock(),
            )


# ---------------------------------------------------------------------------
# RuntimeError guard when openai_client is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_paper_summary_raises_runtime_error_when_client_none(monkeypatch):
    """RuntimeError is raised when both openai_client arg and svc.openai_client are None.

    Guard: if the lifespan never ran (or client was not wired), the function
    must fail fast with a clear message rather than passing None into
    call_llm_structured.
    """
    # Override the autouse fixture: set svc.openai_client to None explicitly.
    monkeypatch.setattr(summarization.svc, "openai_client", None)

    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    pool = _make_pool(conn)

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(RuntimeError, match="openai_client not initialized"):
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=AsyncMock(),
                verifier=MagicMock(),
                embedder=MagicMock(),
                openai_client=None,  # explicit None — both paths exhausted
            )


# ---------------------------------------------------------------------------
# Prompt-shape split: system carries rules, user carries data only
# ---------------------------------------------------------------------------


def test_summarize_prompt_template_contains_no_instruction_head():
    """SUMMARIZE_PROMPT_TEMPLATE carries only data placeholders, no rules.

    The summarisation rules now live in _SYSTEM_SUMMARIZE (system role).
    The template used as the user-role message must not duplicate the rules.
    """
    from paper_ingestion.services.summarization import SUMMARIZE_PROMPT_TEMPLATE

    assert "CRITICAL RULES" not in SUMMARIZE_PROMPT_TEMPLATE
    assert "You are" not in SUMMARIZE_PROMPT_TEMPLATE
    assert "{title}" in SUMMARIZE_PROMPT_TEMPLATE
    assert "{authors}" in SUMMARIZE_PROMPT_TEMPLATE
    assert "{text}" in SUMMARIZE_PROMPT_TEMPLATE


def test_system_summarize_contains_rules():
    """_SYSTEM_SUMMARIZE carries the critical rules in the system constant."""
    from paper_ingestion.services.summarization import _SYSTEM_SUMMARIZE

    assert "CRITICAL RULES" in _SYSTEM_SUMMARIZE
    assert "You are" in _SYSTEM_SUMMARIZE
    assert "verbatim quote" in _SYSTEM_SUMMARIZE.lower() or "verbatim" in _SYSTEM_SUMMARIZE


def test_summary_prompt_shapes_include_relevance_notes():
    """Prompt JSON examples should match the optional relevance_notes schema field."""
    from paper_ingestion.services.summarization import _SYSTEM_REDUCE, _SYSTEM_SUMMARIZE

    assert '"relevance_notes"' in _SYSTEM_SUMMARIZE
    assert '"relevance_notes"' in _SYSTEM_REDUCE
    assert "null" in _SYSTEM_SUMMARIZE
    assert "null" in _SYSTEM_REDUCE


@pytest.mark.asyncio
async def test_generate_paper_summary_uses_arg_client_when_svc_client_none(monkeypatch):
    """When svc.openai_client is None, an explicit openai_client arg is used.

    Companion to test_generate_paper_summary_raises_runtime_error_when_client_none.
    Verifies the positive case: if svc.openai_client is None but an explicit
    client is passed as an argument, the function uses that client without
    raising RuntimeError. The mock client is passed through to call_llm_structured.
    """
    # Override the autouse fixture: set svc.openai_client to None.
    monkeypatch.setattr(summarization.svc, "openai_client", None)

    # Mock the arg client
    arg_client = AsyncMock()

    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = {
        "id": 1,
        "paper_id": 7,
        "summary_brief": "Test brief",
        "summary_detailed": "Test detailed",
        "tldr": "Test TLDR",
        "key_findings": [],
        "cross_references": [],
        "methodology": "Test methodology",
        "limitations": "Test limitations",
        "relevance_notes": "Test notes",
        "confidence": Confidence.MEDIUM,
        "llm_model": "test-model",
        "summary_verified": False,
        "created_at": datetime.now(UTC),
    }
    pool = _make_pool(conn, conn_phase2)

    # Set up a mock LLM output
    llm_output = SummarizationOutput(
        summary="Test summary",
        key_findings=[],
    )

    # Set up the verifier mock
    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=0,
        verified_count=0,
        confidence=Confidence.NONE,
    )

    # Mock call_llm_structured to avoid actual LLM calls and to verify
    # that the arg_client is passed through
    patch_ctx, mock_call_llm = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        # Should NOT raise — uses the arg_client
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=verifier,
            embedder=MagicMock(),
            openai_client=arg_client,
        )

        # Verify the arg_client was passed to call_llm_structured
        mock_call_llm.assert_called_once()
        # The client is passed as the first positional argument to call_llm_structured
        call_args = mock_call_llm.call_args.args
        assert len(call_args) > 0 and call_args[0] is arg_client, (
            f"call_llm_structured must receive arg_client as first positional arg, "
            f"got: {call_args[0] if call_args else 'no args'}"
        )
        assert result is not None


# ---------------------------------------------------------------------------
# degrade contract — abstract substitutes ONLY when the LLM text is empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_summary_substitutes_abstract_only_when_llm_text_empty():
    """Verification failure with EMPTY parsed brief/detailed falls back to the abstract.

    Companion to the confidence-NONE roundtrip test (which pins that non-empty
    LLM text survives). No 'Unable to summarize reliably' boilerplate anywhere.
    """
    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = [_chunk_row()]

    stored_row = {
        "id": 5,
        "paper_id": 7,
        "summary_brief": "Original abstract text.",
        "summary_detailed": "Original abstract text.",
        "tldr": "semantic scholar summary",
        "key_findings": [],
        "methodology": None,
        "limitations": None,
        "relevance_notes": None,
        "confidence": "NONE",
        "cross_references": [],
        "llm_model": "smart-model",
        "summary_verified": False,
        "created_at": datetime.now(UTC),
    }
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = stored_row
    pool = _make_pool(conn_phase1, conn_phase2)

    llm_output = SummarizationOutput(
        tldr="",
        summary_brief="",
        summary_detailed="",
        key_findings=[],
    )

    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=0,
        verified_count=0,
        confidence=Confidence.NONE,
    )

    patch_ctx, _llm_mock = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=verifier,
            embedder=MagicMock(),
        )

    insert_args = conn_phase2.fetchrow.call_args.args
    assert insert_args[2] == "Original abstract text."
    assert insert_args[3] == "Original abstract text."
    assert "Unable to summarize reliably" not in insert_args[2]
    assert result.coverage == 0.0


# ---------------------------------------------------------------------------
# force=True — regenerate over an existing summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_regenerates_existing_summary():
    """force=True skips the idempotency early-return and upserts a fresh summary."""
    conn_phase1 = AsyncMock()
    # Only the paper-existence lookup; the idempotency query must NOT be issued
    # even though a summary row exists in the DB.
    conn_phase1.fetchrow.side_effect = [_paper_row()]
    conn_phase1.fetch.return_value = [_chunk_row()]

    stored_row = {
        "id": 5,
        "paper_id": 7,
        "user_id": 42,
        "summary_brief": "Fresh brief",
        "summary_detailed": "Fresh detailed",
        "tldr": "Fresh tldr",
        "key_findings": [],
        "methodology": None,
        "limitations": None,
        "relevance_notes": None,
        "confidence": "HIGH",
        "cross_references": [],
        "llm_model": "smart",
        "summary_verified": True,
        "created_at": datetime.now(UTC),
    }
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = stored_row
    pool = _make_pool(conn_phase1, conn_phase2)

    llm_output = SummarizationOutput(
        tldr="Fresh tldr",
        summary_brief="Fresh brief",
        summary_detailed="Fresh detailed",
        key_findings=[],
    )

    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=1,
        verified_count=1,
        confidence=Confidence.HIGH,
    )

    patch_ctx, llm_mock = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=verifier,
            embedder=MagicMock(),
            user_id=42,
            force=True,
        )

    llm_mock.assert_called_once()
    assert conn_phase1.fetchrow.call_count == 1, (
        "force=True must skip the idempotency lookup — only the paper row is fetched"
    )
    assert conn_phase2.fetchrow.called, "the upsert must run"
    insert_args = conn_phase2.fetchrow.call_args.args
    assert insert_args[2] == "Fresh brief"
    assert result.summary.summary_brief == "Fresh brief"


@pytest.mark.asyncio
async def test_paper_summarize_job_forwards_force(monkeypatch):
    """_paper_summarize_job reads payload['force'] and forwards it as a keyword."""
    from paper_ingestion.paper_jobs import _paper_summarize_job

    monkeypatch.setattr(summarization.svc, "verifier", MagicMock())
    monkeypatch.setattr(summarization.svc, "embedder", MagicMock())
    # _paper_summarize_job imports generate_paper_summary at call time, so
    # patching the module attribute intercepts the call.
    fake_result = MagicMock()
    fake_result.summary.id = 1
    fake_result.coverage = 1.0
    fake_result.passes = 1
    gen_mock = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(summarization, "generate_paper_summary", gen_mock)

    conn = AsyncMock()
    conn.fetchrow.return_value = {"discovered_by": 42}  # ownership granted
    pool = _make_pool(conn)
    ctx = MagicMock(update_progress=AsyncMock())

    await _paper_summarize_job(
        pool=pool,
        http_client=AsyncMock(),
        payload={"paper_id": 7, "user_id": 42, "force": True},
        ctx=ctx,
    )

    gen_mock.assert_awaited_once()
    assert gen_mock.await_args.kwargs.get("force") is True
    assert gen_mock.await_args.kwargs.get("user_id") == 42


# ---------------------------------------------------------------------------
# map-reduce path — full coverage, window-verified quotes only
# ---------------------------------------------------------------------------


def _chunk_rows(contents: list[str]) -> list[dict]:
    """Build ordered chunk DB rows from a list of chunk contents."""
    return [
        {**_chunk_row(), "id": i + 1, "chunk_index": i, "content": c, "end_char": len(c)}
        for i, c in enumerate(contents)
    ]


def _stored_row(**overrides) -> dict:
    """Return a stored paper_summaries row accepted by row_to_summary_response."""
    row = {
        "id": 5,
        "paper_id": 7,
        "summary_brief": "brief",
        "summary_detailed": "detailed",
        "tldr": "tldr",
        "key_findings": [],
        "methodology": None,
        "limitations": None,
        "relevance_notes": None,
        "confidence": "HIGH",
        "cross_references": [],
        "llm_model": "smart",
        "summary_verified": True,
        "created_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def _window_llm(digests_by_marker: dict, reduce_output: ReduceSummary):
    """Side effect that answers per-window digest, condense, and reduce calls."""

    def side_effect(client, *, response_model, prompt, options=None, config=None, **_):
        if response_model is WindowDigest:
            matches = [d for m, d in digests_by_marker.items() if m in prompt]
            assert len(matches) == 1, f"prompt must contain exactly one window: {prompt[:120]}"
            return matches[0]
        if response_model is CondensedDigest:
            return CondensedDigest(key_points=["merged"])
        if response_model is ReduceSummary:
            return reduce_output
        raise AssertionError(f"unexpected response_model: {response_model}")

    return side_effect


_LONG_PAPER_CONTENTS = [
    "## Alpha\nThe alpha experiment shows a unique alpha result.\n" + "alpha filler " * 140,
    "## Beta\nThe beta experiment shows a distinctive beta outcome.\n" + "beta filler " * 150,
    "## Gamma\nThe gamma experiment shows a singular gamma effect.\n" + "gamma filler " * 140,
]


async def _run_long_paper(monkeypatch, budget_fn, side_effect, stored_row):
    """Drive generate_paper_summary over the three-section long paper."""

    async def _budget(db_pool, reserved_output_tokens):  # noqa: ARG001 — seam is async
        return budget_fn(reserved_output_tokens)

    monkeypatch.setattr(summarization, "_input_char_budget", _budget)

    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = _chunk_rows(_LONG_PAPER_CONTENTS)
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = stored_row
    pool = _make_pool(conn_phase1, conn_phase2)

    patch_ctx, llm_mock = _patched_call_llm(side_effect=side_effect)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=QuoteVerifier(),
            embedder=MagicMock(),
        )
    return result, llm_mock, conn_phase2


@pytest.mark.asyncio
async def test_map_reduce_reads_every_window_and_carries_window_verified_quotes(monkeypatch):
    """Each window gets its own digest call and only window-verified quotes survive.

    The reduce stage cannot mint quotes: stored finding quotes must be a subset
    of the quotes produced (and verified) at the map stage.
    """
    digests = {
        "unique alpha result": WindowDigest(
            key_points=["alpha-point"],
            key_findings=[KeyFindingOutput(finding="alpha finding", quote="unique alpha result")],
        ),
        "distinctive beta outcome": WindowDigest(
            key_points=["beta-point"],
            key_findings=[
                KeyFindingOutput(
                    finding="beta hallucination", quote="this quote was never in the paper"
                )
            ],
        ),
        "singular gamma effect": WindowDigest(
            key_points=["gamma-point"],
            key_findings=[KeyFindingOutput(finding="gamma finding", quote="singular gamma effect")],
        ),
    }
    reduce_output = ReduceSummary(
        tldr="map reduce tldr",
        summary_brief="combined brief",
        summary_detailed="combined detailed",
    )

    result, llm_mock, conn_phase2 = await _run_long_paper(
        monkeypatch,
        lambda reserved: 2000 if reserved == summarization._DIGEST_OUTPUT_TOKENS else 5000,
        _window_llm(digests, reduce_output),
        _stored_row(confidence="MEDIUM", summary_verified=False),
    )

    digest_prompts = [
        c.kwargs["prompt"]
        for c in llm_mock.call_args_list
        if c.kwargs["response_model"] is WindowDigest
    ]
    assert len(digest_prompts) == 3
    for marker in ("unique alpha result", "distinctive beta outcome", "singular gamma effect"):
        assert sum(marker in p for p in digest_prompts) == 1

    reduce_calls = [
        c for c in llm_mock.call_args_list if c.kwargs["response_model"] is ReduceSummary
    ]
    assert len(reduce_calls) == 1
    reduce_prompt = reduce_calls[0].kwargs["prompt"]
    for point in ("alpha-point", "beta-point", "gamma-point"):
        assert point in reduce_prompt
    assert "unique alpha result" in reduce_prompt
    assert llm_mock.call_count == 4

    stored_findings = conn_phase2.fetchrow.call_args.args[5]
    stored_quotes = {f["quote"] for f in stored_findings}
    map_quotes = {f.quote for d in digests.values() for f in d.key_findings}
    assert stored_quotes == {"unique alpha result", "singular gamma effect"}
    assert stored_quotes <= map_quotes
    assert all(f["verified"] for f in stored_findings)
    assert conn_phase2.fetchrow.call_args.args[9] == "MEDIUM"
    assert result.passes == 3
    assert result.coverage == 1.0


@pytest.mark.asyncio
async def test_digest_overflow_goes_through_hierarchical_condense(monkeypatch):
    """Digests exceeding one reduce window are condensed level-wise before the reduce."""
    digests = {
        "unique alpha result": WindowDigest(key_points=["alpha " + "x" * 300]),
        "distinctive beta outcome": WindowDigest(key_points=["beta " + "y" * 300]),
        "singular gamma effect": WindowDigest(key_points=["gamma " + "z" * 300]),
    }
    reduce_output = ReduceSummary(
        tldr="hier tldr",
        summary_brief="hier brief",
        summary_detailed="hier detailed",
    )

    result, llm_mock, conn_phase2 = await _run_long_paper(
        monkeypatch,
        lambda reserved: 2000 if reserved == summarization._DIGEST_OUTPUT_TOKENS else 600,
        _window_llm(digests, reduce_output),
        _stored_row(confidence="LOW", summary_verified=False),
    )

    models_called = [c.kwargs["response_model"] for c in llm_mock.call_args_list]
    assert models_called.count(WindowDigest) == 3
    assert models_called.count(CondensedDigest) == 3
    assert models_called.count(ReduceSummary) == 1
    assert llm_mock.call_count == 7

    reduce_call = next(
        c for c in llm_mock.call_args_list if c.kwargs["response_model"] is ReduceSummary
    )
    assert "merged" in reduce_call.kwargs["prompt"]

    assert conn_phase2.fetchrow.call_args.args[5] == []
    assert conn_phase2.fetchrow.call_args.args[9] == "LOW"
    assert result.passes == 3
    assert result.coverage == 1.0


@pytest.mark.asyncio
async def test_single_window_paper_keeps_single_call_and_prompt_shape():
    """Short papers keep exactly one LLM call with the unchanged prompt and options."""
    from jarvis_common.prompt_safety import max_input_chars, wrap_delimited
    from jarvis_common.settings import get_core_settings

    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = [_chunk_row()]
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = _stored_row()
    pool = _make_pool(conn_phase1, conn_phase2)

    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=1,
        verified_count=1,
        confidence=Confidence.HIGH,
    )

    llm_output = SummarizationOutput(
        tldr="A good paper",
        summary_brief="Brief summary",
        summary_detailed="Detailed summary",
        key_findings=[],
    )
    patch_ctx, llm_mock = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=verifier,
            embedder=MagicMock(),
        )

    llm_mock.assert_called_once()
    kwargs = llm_mock.call_args.kwargs
    assert kwargs["response_model"] is SummarizationOutput

    expected_max = max_input_chars(
        get_core_settings().llm_smart_num_ctx,
        reserved_output_tokens=summarization._SUMMARY_OUTPUT_TOKENS,
    )
    expected_title, _ = wrap_delimited("title", "Test Paper")
    expected_authors, _ = wrap_delimited("authors", "Ada")
    expected_text, truncated = wrap_delimited(
        "paper_text", "This paper improves retrieval quality.", max_chars=expected_max
    )
    assert truncated is False
    assert kwargs["prompt"] == summarization.SUMMARIZE_PROMPT_TEMPLATE.format(
        title=expected_title, authors=expected_authors, text=expected_text
    )
    options = kwargs["options"]
    assert options.system == summarization._SYSTEM_SUMMARIZE
    assert options.max_tokens == summarization._SUMMARY_OUTPUT_TOKENS
    assert result.passes == 1
    assert result.coverage == 1.0


# ---------------------------------------------------------------------------
# map-reduce degraded + condense-cap edge paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_reduce_all_findings_unverified_degrades_to_coverage_zero(monkeypatch):
    """When no window finding verifies AND the reduce text is empty, the summary
    degrades to the abstract fallback with coverage 0.0 — the honest "nothing
    verified" signal the banner reads."""
    digests = {
        "unique alpha result": WindowDigest(
            key_points=["alpha-point"],
            key_findings=[
                KeyFindingOutput(finding="alpha", quote="this quote is absent from alpha")
            ],
        ),
        "distinctive beta outcome": WindowDigest(
            key_points=["beta-point"],
            key_findings=[KeyFindingOutput(finding="beta", quote="this quote is absent from beta")],
        ),
        "singular gamma effect": WindowDigest(
            key_points=["gamma-point"],
            key_findings=[
                KeyFindingOutput(finding="gamma", quote="this quote is absent from gamma")
            ],
        ),
    }
    # Empty brief/detailed forces the abstract substitution (degraded path).
    reduce_output = ReduceSummary(tldr="unverified tldr")

    result, _llm_mock, conn_phase2 = await _run_long_paper(
        monkeypatch,
        lambda reserved: 2000 if reserved == summarization._DIGEST_OUTPUT_TOKENS else 5000,
        _window_llm(digests, reduce_output),
        _stored_row(confidence="LOW", summary_verified=False),
    )

    assert result.coverage == 0.0
    # No verified findings were stored.
    assert conn_phase2.fetchrow.call_args.args[5] == []
    # The degraded summary substituted the abstract for the empty reduce text.
    # Positional INSERT args: [0]=sql, [1]=paper_id, [2]=summary_brief.
    assert conn_phase2.fetchrow.call_args.args[2] == _paper_row()["abstract"]


@pytest.mark.asyncio
async def test_condense_level_cap_warns_but_still_returns_reduce_summary(monkeypatch, caplog):
    """A reduce budget so tight the digests can never shrink to one window hits the
    level cap: a WARNING fires and the reduce summary is still produced (no crash)."""
    import logging

    # Level-0 digests are large (rendered key points), so they overflow the tiny
    # reduce budget and force the condense loop to run.
    digests = {
        "unique alpha result": WindowDigest(key_points=["alpha-point " + "A" * 400]),
        "distinctive beta outcome": WindowDigest(key_points=["beta-point " + "B" * 400]),
        "singular gamma effect": WindowDigest(key_points=["gamma-point " + "G" * 400]),
    }
    reduce_output = ReduceSummary(
        tldr="capped tldr",
        summary_brief="capped brief",
        summary_detailed="capped detailed",
    )

    # Condense never shrinks: each merged digest stays large enough that the
    # regroup keeps producing multiple groups, so the loop runs to the level cap.
    _big = "C" * 400

    def side_effect(client, *, response_model, prompt, options=None, config=None, **_):
        if response_model is WindowDigest:
            matches = [d for m, d in digests.items() if m in prompt]
            assert len(matches) == 1
            return matches[0]
        if response_model is CondensedDigest:
            return CondensedDigest(key_points=[_big])
        if response_model is ReduceSummary:
            return reduce_output
        raise AssertionError(f"unexpected response_model: {response_model}")

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.summarization"):
        result, llm_mock, conn_phase2 = await _run_long_paper(
            monkeypatch,
            # Digest budget large (3 windows); reduce budget tiny so digests never fit.
            lambda reserved: 2000 if reserved == summarization._DIGEST_OUTPUT_TOKENS else 700,
            side_effect,
            _stored_row(confidence="LOW", summary_verified=False),
        )

    # The reduce summary was still produced despite the truncated input — its
    # brief was written to the DB (positional INSERT arg [2]).
    reduce_calls = [
        c for c in llm_mock.call_args_list if c.kwargs["response_model"] is ReduceSummary
    ]
    assert len(reduce_calls) == 1
    assert result.passes == 3
    assert conn_phase2.fetchrow.call_args.args[2] == "capped brief"
    # A truncation/cap warning fired (either the level cap or the regroup floor).
    assert any("truncated" in r.message and r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Snapshot linking for verified findings
# ---------------------------------------------------------------------------


def _snapshot_link_harness(llm_output: SummarizationOutput):
    """Pool, capture-dict, and verifier that marks every finding verified."""
    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = [_chunk_row()]
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = _stored_row()
    pool = _make_pool(conn_phase1, conn_phase2)

    captured: dict = {}

    def _verify(findings, full_text, chunks):
        captured["findings"] = findings
        for f in findings:
            f.verified = True
        return SimpleNamespace(
            total_findings=len(findings), verified_count=len(findings), confidence=Confidence.HIGH
        )

    verifier = MagicMock()
    verifier.verify_findings.side_effect = _verify
    return pool, captured, verifier


@pytest.mark.asyncio
async def test_verified_finding_links_relative_snapshot_path():
    """A verified finding with a positive page number links a base-relative snapshot path."""
    llm_output = SummarizationOutput(
        tldr="A good paper",
        summary_brief="Brief summary",
        summary_detailed="Detailed summary",
        key_findings=[KeyFindingOutput(finding="F", quote="Q", page_number=3)],
    )
    pool, captured, verifier = _snapshot_link_harness(llm_output)

    patch_ctx, _llm_mock = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=verifier,
            embedder=MagicMock(),
        )

    assert captured["findings"][0].snapshot_path == "7/page_3.png"


@pytest.mark.asyncio
async def test_snapshot_link_skipped_when_candidate_escapes_base(monkeypatch):
    """A snapshot candidate rejected by the traversal guard is skipped, never linked."""
    llm_output = SummarizationOutput(
        tldr="A good paper",
        summary_brief="Brief summary",
        summary_detailed="Detailed summary",
        key_findings=[KeyFindingOutput(finding="F", quote="Q", page_number=3)],
    )
    pool, captured, verifier = _snapshot_link_harness(llm_output)

    def _reject(*args, **kwargs):
        raise ValueError("path escapes base directory")

    monkeypatch.setattr(summarization, "secure_path", _reject)

    patch_ctx, _llm_mock = _patched_call_llm(return_value=llm_output)
    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
        patch_ctx,
    ):
        await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=AsyncMock(),
            verifier=verifier,
            embedder=MagicMock(),
        )

    assert captured["findings"][0].snapshot_path is None
