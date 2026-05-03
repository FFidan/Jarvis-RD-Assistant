"""Unit tests for the summarization service module."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models /
# rapidfuzz stubs.
from paper_ingestion.models import Confidence
from paper_ingestion.services import summarization


@asynccontextmanager
async def _noop_lock(*args, **kwargs):
    yield


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


@pytest.mark.asyncio
async def test_find_cross_references_prefers_semantic_results():
    """Semantic search results are deduplicated and preferred over keyword fallback."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": "Abstract"}
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
async def test_find_cross_references_falls_back_to_keyword_overlap():
    """Keyword fallback is used when semantic search fails."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"abstract": "Abstract"}
    conn.fetch.return_value = [{"id": 8, "title": "Retrieval Agents"}]
    embedder = AsyncMock()
    embedder.search_similar.side_effect = RuntimeError("qdrant down")

    result = await summarization._find_cross_references(
        conn,
        7,
        "Retrieval Agents Systems",
        embedder=embedder,
    )

    assert len(result) == 1
    assert result[0].relationship == "potential_overlap"
    assert result[0].related_paper_id == 8


@pytest.mark.asyncio
async def test_generate_paper_summary_returns_existing_summary():
    """Existing summaries short-circuit before any LLM call."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), {"id": 1}]
    pool = _make_pool(conn)
    verifier = MagicMock()
    embedder = MagicMock()
    http_client = AsyncMock()

    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(
            summarization, "row_to_summary_response", return_value="existing-summary"
        ) as convert,
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=http_client,
            verifier=verifier,
            embedder=embedder,
        )

    assert result == "existing-summary"
    http_client.post.assert_not_called()
    convert.assert_called_once()


@pytest.mark.asyncio
async def test_generate_paper_summary_raises_on_invalid_llm_json():
    """Malformed JSON from the LLM becomes HTTP 502."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)

    http_response = MagicMock()
    http_response.raise_for_status.return_value = None
    http_response.json.return_value = {
        "choices": [{"message": {"content": "{not-json"}}],
        "model": "smart",
    }
    http_client = AsyncMock()
    http_client.post.return_value = http_response

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="LLM returned invalid JSON") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_generate_paper_summary_rejects_non_string_message_content():
    """Schema-invalid LiteLLM content payloads should return a stable HTTP 502."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)

    http_response = MagicMock()
    http_response.raise_for_status.return_value = None
    http_response.json.return_value = {
        "choices": [{"message": {"content": {"not": "text"}}}],
        "model": "smart",
    }
    http_client = AsyncMock()
    http_client.post.return_value = http_response

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="Malformed LLM response") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_generate_paper_summary_rejects_non_list_key_findings():
    """A dict-valued key_findings payload should not crash the summarizer."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)

    http_response = MagicMock()
    http_response.raise_for_status.return_value = None
    http_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "tldr": "short tldr",
                            "summary_brief": "brief",
                            "summary_detailed": "detailed",
                            "key_findings": {"finding": "wrong-shape"},
                        }
                    )
                }
            }
        ],
        "model": "smart",
    }
    http_client = AsyncMock()
    http_client.post.return_value = http_response

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="Malformed LLM response") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_generate_paper_summary_rejects_non_dict_finding_items():
    """A list containing non-dict findings should map to a stable HTTP 502."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)

    http_response = MagicMock()
    http_response.raise_for_status.return_value = None
    http_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary_brief": "brief",
                            "summary_detailed": "detailed",
                            "key_findings": ["bad-item"],
                        }
                    )
                }
            }
        ],
        "model": "smart",
    }
    http_client = AsyncMock()
    http_client.post.return_value = http_response

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="Malformed LLM response") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_generate_paper_summary_raises_on_missing_paper():
    """A missing paper should return HTTP 404 before any LLM call."""
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    pool = _make_pool(conn)
    http_client = AsyncMock()

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="Paper not found") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 404
    http_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_generate_paper_summary_raises_on_missing_chunks():
    """Papers without processed chunks should return HTTP 400."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = []
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)
    http_client = AsyncMock()

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="process-pdf first") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 400
    http_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_generate_paper_summary_maps_read_timeout_to_504():
    """ReadTimeout from LiteLLM should map to a stable HTTP 504."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)
    http_client = AsyncMock()
    http_client.post.side_effect = summarization.httpx.ReadTimeout("slow")

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="timed out") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 504


@pytest.mark.asyncio
async def test_generate_paper_summary_maps_http_status_error_to_502():
    """HTTP status failures from LiteLLM should map to HTTP 502."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_paper_row(), None]
    conn.fetch.return_value = [_chunk_row()]
    conn.fetchval.return_value = "smart"
    pool = _make_pool(conn)
    request = summarization.httpx.Request("POST", "http://litellm.test/v1/chat/completions")
    response = summarization.httpx.Response(500, request=request)
    http_client = AsyncMock()
    http_client.post.side_effect = summarization.httpx.HTTPStatusError(
        "boom",
        request=request,
        response=response,
    )

    with patch.object(summarization, "advisory_lock", _noop_lock):
        with pytest.raises(HTTPException, match="LLM API error") as exc_info:
            await summarization.generate_paper_summary(
                paper_id=7,
                db_pool=pool,
                http_client=http_client,
                verifier=MagicMock(),
                embedder=MagicMock(),
            )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_generate_paper_summary_falls_back_to_abstract_when_verification_fails():
    """Zero verified findings triggers abstract fallback and unverified summary storage."""
    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = [_chunk_row()]
    conn_phase1.fetchval.return_value = "smart"

    stored_row = {
        "id": 3,
        "paper_id": 7,
        "summary_brief": "Unable to summarize reliably. Original abstract: Original abstract text.",
        "summary_detailed": "Original abstract text.",
        "tldr": "short tldr",
        "key_findings": [],
        "methodology": None,
        "limitations": None,
        "relevance_notes": None,
        "confidence": "LOW",
        "cross_references": [],
        "llm_model": "smart-model",
        "summary_verified": False,
        "created_at": datetime.now(UTC),
    }
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = stored_row
    pool = _make_pool(conn_phase1, conn_phase2)

    http_response = MagicMock()
    http_response.raise_for_status.return_value = None
    http_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "tldr": "short tldr",
                            "summary_brief": "draft brief",
                            "summary_detailed": "draft detailed",
                            "key_findings": [
                                {
                                    "finding": "Claim",
                                    "quote": "missing quote",
                                    "page_number": 1,
                                }
                            ],
                        }
                    )
                }
            }
        ],
        "model": "smart-model",
    }
    http_client = AsyncMock()
    http_client.post.return_value = http_response

    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=1,
        verified_count=0,
        confidence=Confidence.LOW,
    )

    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=http_client,
            verifier=verifier,
            embedder=MagicMock(),
        )

    assert result.summary_verified is False
    assert result.summary_detailed == "Original abstract text."
    insert_args = conn_phase2.fetchrow.await_args.args
    assert insert_args[2].startswith("Unable to summarize reliably.")
    assert insert_args[9] == "LOW"


@pytest.mark.asyncio
async def test_generate_paper_summary_falls_back_when_llm_returns_no_findings():
    """No LLM findings should trigger the abstract fallback without crashing."""
    conn_phase1 = AsyncMock()
    conn_phase1.fetchrow.side_effect = [_paper_row(), None]
    conn_phase1.fetch.return_value = [_chunk_row()]
    conn_phase1.fetchval.return_value = "smart"

    stored_row = {
        "id": 4,
        "paper_id": 7,
        "summary_brief": "Unable to summarize reliably (no verifiable findings). Original abstract: Original abstract text.",
        "summary_detailed": "Original abstract text.",
        "tldr": "semantic scholar summary",
        "key_findings": [],
        "methodology": None,
        "limitations": None,
        "relevance_notes": None,
        "confidence": "LOW",
        "cross_references": [],
        "llm_model": "smart-model",
        "summary_verified": False,
        "created_at": datetime.now(UTC),
    }
    conn_phase2 = AsyncMock()
    conn_phase2.fetchrow.return_value = stored_row
    pool = _make_pool(conn_phase1, conn_phase2)

    http_response = MagicMock()
    http_response.raise_for_status.return_value = None
    http_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary_brief": "draft brief",
                            "summary_detailed": "draft detailed",
                            "key_findings": [],
                        }
                    )
                }
            }
        ],
        "model": "smart-model",
    }
    http_client = AsyncMock()
    http_client.post.return_value = http_response

    verifier = MagicMock()
    verifier.verify_findings.return_value = SimpleNamespace(
        total_findings=0,
        verified_count=0,
        confidence=Confidence.LOW,
    )

    with (
        patch.object(summarization, "advisory_lock", _noop_lock),
        patch.object(summarization, "_find_cross_references", AsyncMock(return_value=[])),
    ):
        result = await summarization.generate_paper_summary(
            paper_id=7,
            db_pool=pool,
            http_client=http_client,
            verifier=verifier,
            embedder=MagicMock(),
        )

    assert result.summary_verified is False
    assert result.tldr == "semantic scholar summary"
    insert_args = conn_phase2.fetchrow.await_args.args
    assert insert_args[2].startswith("Unable to summarize reliably (no verifiable findings).")
    assert insert_args[5] == []
