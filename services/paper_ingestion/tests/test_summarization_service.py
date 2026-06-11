"""Unit tests for the summarization service module."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models /
# rapidfuzz stubs.
from paper_ingestion.exceptions import EmptyChunksError, LLMError, PaperNotFoundError
from paper_ingestion.models import Confidence
from paper_ingestion.services import summarization
from paper_ingestion.services.summarization_models import SummarizationOutput


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

    assert result == "existing-summary"
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

    assert result.confidence == Confidence.NONE
    assert result.summary_verified is False

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
async def test_cross_references_semantic_path_passes_owner_id():
    """discovered_by is fetched and bound as user_id so the search is user-scoped.

    Empty semantic results stay empty — no fallback query is issued.
    """
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": None, "discovered_by": 99}
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
    conn.fetch.assert_not_called()


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

    BUG-SUMMARIZER-1 fix (commit 60d9b36d): call_llm_structured raising a
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
# PI-07 — prompt-shape split: system carries rules, user carries data only
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
        await summarization.generate_paper_summary(
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
    assert result.summary_brief == "Fresh brief"


@pytest.mark.asyncio
async def test_paper_summarize_job_forwards_force(monkeypatch):
    """_paper_summarize_job reads payload['force'] and forwards it as a keyword."""
    from paper_ingestion.paper_jobs import _paper_summarize_job

    monkeypatch.setattr(summarization.svc, "verifier", MagicMock())
    monkeypatch.setattr(summarization.svc, "embedder", MagicMock())
    # _paper_summarize_job imports generate_paper_summary at call time, so
    # patching the module attribute intercepts the call.
    gen_mock = AsyncMock(return_value=MagicMock(id=1))
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
