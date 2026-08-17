"""Contract tests for RAG LLM call migration (CFG-RAGNON-1).

Verifies that ask_paper and ask_cross_paper use call_llm_structured (via the
module-level _call_rag_llm helper) rather than the legacy
request_chat_completion_content path.

Pattern: httpx.AsyncClient(ASGITransport) + app.dependency_overrides — same
boundary-adapter shape as test_rag_authorization.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

from paper_ingestion.models.rag import AskResponse
from tests.conftest import _make_pool_and_conn


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_rag_authorization.py)
# ---------------------------------------------------------------------------


@contextmanager
def _wired_overrides(app, pool):
    """Wire this module's dependency overrides on *app*, restored exactly on exit."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import (
        get_db_pool,
        get_embedder,
        get_http_client,
        get_verifier,
        limiter,
    )

    mock_embedder = AsyncMock()
    mock_embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    mock_http_client = AsyncMock(spec=httpx.AsyncClient)
    mock_verifier = MagicMock()

    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            override_db_dependency=True,
            dependency_overrides={
                verify_api_key: lambda: None,
                get_embedder: lambda: mock_embedder,
                get_http_client: lambda: mock_http_client,
                get_verifier: lambda: mock_verifier,
            },
        ),
    ):
        yield


def _asgi_client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_FAKE_ASK_RESPONSE = AskResponse(answer="structured answer", sources=[])
_FAKE_MESSAGES = [{"role": "user", "content": "q"}]
_FAKE_SOURCES: list[dict] = []


# ---------------------------------------------------------------------------
# Test 1: ask_paper uses _call_rag_llm (i.e. call_llm_structured path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_paper_uses_structured_llm():
    """POST /api/papers/{id}/ask must call _call_rag_llm (call_llm_structured path).

    Evidenced by CFG-RAGNON-1: rag.py replaces request_chat_completion_content
    with call_llm_structured via the @observe()-decorated _call_rag_llm helper.
    """
    from paper_ingestion.main import app

    pool, _conn = _make_pool_and_conn()
    with _wired_overrides(app, pool):
        async with _asgi_client(app) as client:
            with (
                patch(
                    "paper_ingestion.routers.rag.assert_paper_ownership",
                    return_value=None,
                ),
                patch(
                    "paper_ingestion.routers.rag.prepare_single_paper_rag",
                    new=AsyncMock(return_value=(_FAKE_MESSAGES, _FAKE_SOURCES)),
                ),
                patch(
                    "paper_ingestion.routers.rag._call_rag_llm",
                    new=AsyncMock(return_value=_FAKE_ASK_RESPONSE),
                ) as mock_llm,
            ):
                resp = await client.post(
                    "/api/papers/42/ask",
                    json={"question": "what is this about"},
                )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert mock_llm.called, "ask_paper must invoke _call_rag_llm (call_llm_structured path)"
    data = resp.json()
    assert data["answer"] == "structured answer"


# ---------------------------------------------------------------------------
# Test 2: ask_cross_paper uses _call_rag_llm (i.e. call_llm_structured path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_cross_paper_uses_structured_llm():
    """POST /api/ask must call _call_rag_llm (call_llm_structured path).

    Evidenced by CFG-RAGNON-1: same migration applied to ask_cross_paper.
    """
    from paper_ingestion.main import app
    from paper_ingestion.rag.streaming import CrossPaperRagPrep

    pool, _conn = _make_pool_and_conn()

    fake_rag_result = CrossPaperRagPrep(
        messages=_FAKE_MESSAGES,
        sources=_FAKE_SOURCES,
    )

    with _wired_overrides(app, pool):
        async with _asgi_client(app) as client:
            with (
                patch(
                    "paper_ingestion.routers.rag.prepare_cross_paper_rag",
                    new=AsyncMock(return_value=fake_rag_result),
                ),
                patch(
                    "paper_ingestion.routers.rag._call_rag_llm",
                    new=AsyncMock(return_value=_FAKE_ASK_RESPONSE),
                ) as mock_llm,
            ):
                resp = await client.post(
                    "/api/ask",
                    json={"question": "cross-paper question"},
                )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert mock_llm.called, "ask_cross_paper must invoke _call_rag_llm (call_llm_structured path)"
    data = resp.json()
    assert data["answer"] == "structured answer"
