"""Tests for RAG endpoint authorization and upstream error branches.

Covers gaps from B-RAGTEST audit:
- Cross-user ownership denial on ask_paper / ask_paper_stream  (403)
- LLM timeout → 504 on non-streaming ask_paper
- LLM runtime error → 502 on non-streaming ask_paper
- Empty SSE results on ask_paper_stream  (200 + done event)

Pattern: httpx.AsyncClient(ASGITransport) + app.dependency_overrides so tests
run fully in-process without a live database or LiteLLM.  The autouse
``_default_authenticated_user`` fixture in conftest.py already resolves
``get_current_user_id`` to user 1 for every test here.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

from tests.conftest import _make_pool_and_conn


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextmanager
def _wired_overrides(app, pool):
    """Wire this module's dependency overrides on *app*, restored exactly on exit.

    The conftest autouse overrides are never touched: the helper records and
    restores only the keys named here.
    """
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


# ---------------------------------------------------------------------------
# Test 1: ask_paper ownership denial → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_paper_rejects_other_users_paper():
    """POST /api/papers/{id}/ask returns 403 when the paper belongs to another user."""
    from paper_ingestion.main import app

    pool, _conn = _make_pool_and_conn()
    with _wired_overrides(app, pool):
        async with _asgi_client(app) as client:
            with patch(
                "paper_ingestion.routers.rag.assert_paper_ownership",
                side_effect=HTTPException(
                    status_code=403, detail="paper not owned by current user"
                ),
            ):
                resp = await client.post(
                    "/api/papers/999/ask",
                    json={"question": "what is this about"},
                )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test 2: ask_paper_stream ownership denial → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_paper_stream_rejects_other_users_paper():
    """POST /api/papers/{id}/ask/stream returns 403 when paper belongs to another user."""
    from paper_ingestion.main import app

    pool, _conn = _make_pool_and_conn()
    with _wired_overrides(app, pool):
        async with _asgi_client(app) as client:
            with patch(
                "paper_ingestion.routers.rag.assert_paper_ownership",
                side_effect=HTTPException(
                    status_code=403, detail="paper not owned by current user"
                ),
            ):
                resp = await client.post(
                    "/api/papers/999/ask/stream",
                    json={"question": "what is this about"},
                )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test 3: ask_paper → 504 on LLM timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_paper_returns_504_on_llm_timeout():
    """POST /api/papers/{id}/ask returns 504 when LiteLLM times out.

    Evidenced by rag.py try/except around _call_rag_llm:
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="LLM request timed out")
    """
    from paper_ingestion.main import app

    pool, _conn = _make_pool_and_conn()
    fake_messages = [{"role": "user", "content": "q"}]
    fake_sources: list[dict] = []

    with _wired_overrides(app, pool):
        async with _asgi_client(app) as client:
            with (
                patch(
                    "paper_ingestion.routers.rag.assert_paper_ownership",
                    return_value=None,
                ),
                patch(
                    "paper_ingestion.routers.rag.prepare_single_paper_rag",
                    new=AsyncMock(return_value=(fake_messages, fake_sources)),
                ),
                patch(
                    "paper_ingestion.routers.rag._call_rag_llm",
                    new=AsyncMock(side_effect=httpx.TimeoutException("timeout")),
                ),
            ):
                resp = await client.post(
                    "/api/papers/42/ask",
                    json={"question": "what is this about"},
                )

    assert resp.status_code == 504


# ---------------------------------------------------------------------------
# Test 4: ask_paper → 502 on generic LLM error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_paper_returns_502_on_llm_error():
    """POST /api/papers/{id}/ask returns 502 when LiteLLM raises a runtime error.

    Evidenced by rag.py try/except around _call_rag_llm:
        except Exception as exc:
            ...
            raise HTTPException(status_code=502, detail="LLM request failed") from exc
    """
    from paper_ingestion.main import app

    pool, _conn = _make_pool_and_conn()
    fake_messages = [{"role": "user", "content": "q"}]
    fake_sources: list[dict] = []

    with _wired_overrides(app, pool):
        async with _asgi_client(app) as client:
            with (
                patch(
                    "paper_ingestion.routers.rag.assert_paper_ownership",
                    return_value=None,
                ),
                patch(
                    "paper_ingestion.routers.rag.prepare_single_paper_rag",
                    new=AsyncMock(return_value=(fake_messages, fake_sources)),
                ),
                patch(
                    "paper_ingestion.routers.rag._call_rag_llm",
                    new=AsyncMock(side_effect=RuntimeError("LLM backend unavailable")),
                ),
            ):
                resp = await client.post(
                    "/api/papers/42/ask",
                    json={"question": "what is this about"},
                )

    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Test 5: ask_paper_stream emits done event when stream yields only done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_paper_stream_emits_done_with_empty_results():
    """POST /api/papers/{id}/ask/stream returns 200 and a done SSE event.

    Patches stream_rag_events (imported into paper_ingestion.rag.streaming)
    so that it yields only the terminal done event — simulating zero matching
    chunks.  The endpoint wraps it in a StreamingResponse (200) regardless.
    """
    from paper_ingestion.main import app

    pool, _conn = _make_pool_and_conn()
    fake_messages = [{"role": "user", "content": "q"}]
    fake_sources: list[dict] = []

    async def _empty_stream(*args, **kwargs):
        yield b'data: {"type": "done"}\n\n'

    with _wired_overrides(app, pool):
        async with _asgi_client(app) as client:
            with (
                patch(
                    "paper_ingestion.routers.rag.assert_paper_ownership",
                    return_value=None,
                ),
                patch(
                    "paper_ingestion.routers.rag.prepare_single_paper_rag",
                    new=AsyncMock(return_value=(fake_messages, fake_sources)),
                ),
                patch(
                    "paper_ingestion.routers.rag.stream_rag_events",
                    side_effect=_empty_stream,
                ),
            ):
                resp = await client.post(
                    "/api/papers/42/ask/stream",
                    json={"question": "something with zero matching chunks"},
                )

    assert resp.status_code == 200
    assert b"done" in resp.content
