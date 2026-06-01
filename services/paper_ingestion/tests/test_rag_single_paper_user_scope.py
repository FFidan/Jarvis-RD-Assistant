"""``ask_paper`` forwards ``user_id`` to ``prepare_single_paper_rag``.

Boundary-adapter test: patches the prepared-RAG helper to capture kwargs, then
hits the ``/api/papers/{id}/ask`` route via the in-process ASGI transport.  The
test asserts only that ``user_id`` is forwarded as a kwarg; the broader RAG
behaviour is covered by the existing ``test_rag_authorization`` /
``test_rag_contract`` suites.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from paper_ingestion.rag.streaming import prepare_single_paper_rag
from tests.conftest import _make_pool_and_conn


def test_prepare_single_paper_rag_accepts_user_id() -> None:
    sig = inspect.signature(prepare_single_paper_rag)
    assert "user_id" in sig.parameters, "prepare_single_paper_rag must accept user_id kwarg"
    param = sig.parameters["user_id"]
    assert param.default is None, "user_id default must be None"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "user_id should be keyword-only (matches prepare_cross_paper_rag shape)"
    )


@pytest.mark.asyncio
async def test_ask_paper_forwards_user_id_to_prepare_single_paper_rag() -> None:
    """Route handler must pass authenticated user_id into the RAG helper."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import (
        get_db_pool,
        get_embedder,
        get_http_client,
        get_verifier,
    )
    from paper_ingestion.main import app
    from paper_ingestion.models.rag import AskResponse

    pool, _conn = _make_pool_and_conn()
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_http_client] = lambda: AsyncMock(spec=httpx.AsyncClient)
    app.dependency_overrides[get_embedder] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: AsyncMock()
    app.dependency_overrides[verify_api_key] = lambda: None

    try:
        with (
            patch(
                "paper_ingestion.routers.rag.assert_paper_ownership",
                new_callable=AsyncMock,
            ),
            patch(
                "paper_ingestion.routers.rag.prepare_single_paper_rag",
                new_callable=AsyncMock,
            ) as mock_prep,
            patch(
                "paper_ingestion.routers.rag._call_rag_llm",
                new_callable=AsyncMock,
                return_value=AskResponse(answer="answer text", sources=[]),
            ),
        ):
            mock_prep.return_value = ([{"role": "user", "content": "x"}], [])
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/papers/42/ask",
                    json={"question": "hi", "max_chunks": 5},
                )
    finally:
        for dep in (
            get_db_pool,
            verify_api_key,
            get_embedder,
            get_http_client,
            get_verifier,
        ):
            app.dependency_overrides.pop(dep, None)

    assert resp.status_code == 200, resp.text
    assert mock_prep.await_count == 1
    assert mock_prep.await_args is not None
    forwarded_user_id = mock_prep.await_args.kwargs.get("user_id")
    assert forwarded_user_id is not None, (
        "ask_paper must forward user_id to prepare_single_paper_rag"
    )
