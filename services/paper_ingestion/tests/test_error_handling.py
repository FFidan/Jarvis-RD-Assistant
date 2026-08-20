"""Tests for error handling in embedder and search chunks."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
from paper_ingestion.ingestion.embedder import Embedder
from qdrant_client.http.exceptions import ResponseHandlingException


# ---------------------------------------------------------------------------
# embed_texts error handling
# ---------------------------------------------------------------------------


async def test_embed_texts_timeout():
    """embed_texts wraps httpx.TimeoutException as RuntimeError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    mock_http.post.side_effect = httpx.TimeoutException("read timed out")

    with pytest.raises(RuntimeError, match="Embedding service timed out"):
        await embedder.embed_texts(["test text"])


async def test_embed_texts_connection_error():
    """embed_texts wraps httpx.ConnectError as RuntimeError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    mock_http.post.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(RuntimeError, match="Embedding service unavailable"):
        await embedder.embed_texts(["test text"])


# ---------------------------------------------------------------------------
# search_chunks error handling — QdrantUnavailableError (not silent swallow)
# ---------------------------------------------------------------------------


async def test_search_chunks_in_paper_qdrant_transport_error_raises():
    """search_chunks_in_paper raises QdrantUnavailableError on qdrant transport errors."""
    from paper_ingestion.rag.exceptions import QdrantUnavailableError

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    mock_qdrant.query_points.side_effect = ResponseHandlingException(
        ConnectionError("Qdrant connection lost")
    )

    with pytest.raises(QdrantUnavailableError):
        await embedder.search_chunks_in_paper(
            query_text="test query",
            paper_id=1,
            limit=5,
            score_threshold=0.3,
        )


async def test_search_chunks_global_qdrant_transport_error_raises():
    """search_chunks_global raises QdrantUnavailableError on qdrant transport errors."""
    from paper_ingestion.rag.exceptions import QdrantUnavailableError

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    mock_qdrant.query_points.side_effect = ResponseHandlingException(
        ConnectionError("Qdrant connection lost")
    )

    with pytest.raises(QdrantUnavailableError):
        await embedder.search_chunks_global(
            query_text="test query",
            limit=10,
            score_threshold=0.2,
        )


async def test_search_similar_qdrant_transport_error_raises():
    """A lost Qdrant connection must surface as a retriable unavailability,
    not a raw transport error, so /api/similar can answer 503 instead of 500."""
    from paper_ingestion.rag.exceptions import QdrantUnavailableError

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    mock_qdrant.query_points.side_effect = ResponseHandlingException(
        ConnectionError("Qdrant connection lost")
    )

    with pytest.raises(QdrantUnavailableError):
        await embedder.search_similar(
            query_text="test query",
            limit=5,
            score_threshold=0.6,
        )


async def test_search_chunks_runtime_error_propagates():
    """RuntimeError from embed_texts propagates through search_chunks_in_paper."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    embedder.embed_texts = AsyncMock(side_effect=RuntimeError("Embedding service timed out"))

    with pytest.raises(RuntimeError, match="Embedding service timed out"):
        await embedder.search_chunks_in_paper(
            query_text="test query",
            paper_id=1,
            limit=5,
        )


# ---------------------------------------------------------------------------
# Route-level: Ask endpoints return 503 / degraded SSE on Qdrant transport error
# ---------------------------------------------------------------------------


@pytest.fixture()
def _qdrant_error_env(monkeypatch):
    """Patch prepare_single_paper_rag and prepare_cross_paper_rag to raise QdrantUnavailableError."""
    from paper_ingestion.rag.exceptions import QdrantUnavailableError

    monkeypatch.setattr(
        "paper_ingestion.routers.rag.prepare_single_paper_rag",
        AsyncMock(side_effect=QdrantUnavailableError("Qdrant down")),
    )
    monkeypatch.setattr(
        "paper_ingestion.routers.rag.prepare_cross_paper_rag",
        AsyncMock(side_effect=QdrantUnavailableError("Qdrant down")),
    )


@pytest.fixture()
def _qdrant_error_env_cross(monkeypatch):
    """Patch only prepare_cross_paper_rag to raise QdrantUnavailableError."""
    from paper_ingestion.rag.exceptions import QdrantUnavailableError

    monkeypatch.setattr(
        "paper_ingestion.routers.rag.prepare_cross_paper_rag",
        AsyncMock(side_effect=QdrantUnavailableError("Qdrant down")),
    )


@pytest.fixture()
def _empty_chunks_env(monkeypatch):
    """Patch prepare_single_paper_rag to raise NoRelevantChunksError (genuine empty hit)."""
    from paper_ingestion.rag.exceptions import NoRelevantChunksError

    monkeypatch.setattr(
        "paper_ingestion.routers.rag.prepare_single_paper_rag",
        AsyncMock(side_effect=NoRelevantChunksError("No relevant passages found.")),
    )


@pytest.fixture()
def _patched_app_ownership(monkeypatch):
    """Skip ownership assertion so we can test ask routes without DB."""
    monkeypatch.setattr(
        "paper_ingestion.routers.rag.assert_paper_ownership",
        AsyncMock(return_value=None),
    )


def _make_fake_pool():
    """Return an asyncpg.Pool stand-in whose acquire() is an async context manager."""
    fake_conn = AsyncMock()
    fake_pool = MagicMock()
    # acquire() must return an object that supports `async with`
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return fake_pool


@pytest.fixture()
def _stub_rag_deps():
    """Override FastAPI deps that read from app.state so route tests work without lifespan."""
    from paper_ingestion.deps import get_db_pool, get_embedder, get_http_client, limiter
    from paper_ingestion.main import app

    fake_embedder = AsyncMock()
    fake_http = AsyncMock(spec=httpx.AsyncClient)
    fake_pool = _make_fake_pool()

    # pool=None: these route tests resolve everything through dependency
    # overrides and must leave ``app.state`` untouched.
    with patch_pi_test_app(
        None,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            dependency_overrides={
                get_embedder: lambda: fake_embedder,
                get_http_client: lambda: fake_http,
                get_db_pool: lambda: fake_pool,
            },
        ),
    ):
        yield


# ---- ask_paper (non-stream): Qdrant error → 503 ----


async def test_ask_paper_qdrant_error_returns_503(
    _configure_api_key,
    _patched_app_ownership,
    _qdrant_error_env,
    _stub_rag_deps,
):
    """ask_paper returns 503 when Qdrant raises a transport error."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/papers/1/ask",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )
    assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"


async def test_ask_paper_preparation_error_returns_502_and_logs_admin_event(
    _configure_api_key,
    _patched_app_ownership,
    _stub_rag_deps,
    monkeypatch,
    caplog,
):
    """Unexpected single-paper preparation errors are controlled and admin-visible."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    log_event_mock = AsyncMock()
    monkeypatch.setattr(
        "paper_ingestion.routers.rag.prepare_single_paper_rag",
        AsyncMock(side_effect=RuntimeError("provider returned 500 with marker abc123")),
    )
    monkeypatch.setattr("paper_ingestion.routers.rag.log_event", log_event_mock, raising=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/papers/1/ask",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )

    assert resp.status_code == 502
    assert resp.json()["detail"] == {
        "status": "degraded",
        "code": "rag_preparation_failed",
        "message": "RAG preparation failed.",
    }
    assert "abc123" not in resp.text
    assert "abc123" not in caplog.text
    log_event_mock.assert_awaited_once()
    assert log_event_mock.await_args.kwargs["context"] == {
        "endpoint": "single_paper",
        "phase": "preparation",
        "status_code": 502,
        "exception_class": "RuntimeError",
    }


# ---- ask_paper_stream (stream): Qdrant error → degraded SSE frame ----


async def test_ask_paper_stream_qdrant_error_returns_degraded_sse(
    _configure_api_key,
    _patched_app_ownership,
    _qdrant_error_env,
    _stub_rag_deps,
):
    """ask_paper_stream emits a degraded SSE error frame (retriable:true) on Qdrant error."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/papers/1/ask/stream",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )
    assert resp.status_code == 200
    body = resp.text
    found_retriable = False
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
                if payload.get("type") == "error" and payload.get("retriable") is True:
                    found_retriable = True
                    break
            except json.JSONDecodeError:
                pass
    assert found_retriable, f"No retriable error SSE event found in: {body!r}"


# ---- ask_cross_paper (non-stream): Qdrant error → 503 ----


async def test_ask_cross_paper_qdrant_error_returns_503(
    _configure_api_key,
    _qdrant_error_env_cross,
    _stub_rag_deps,
):
    """ask_cross_paper returns 503 when Qdrant raises a transport error."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/ask",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )
    assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"


async def test_ask_cross_paper_preparation_error_returns_502_and_logs_admin_event(
    _configure_api_key,
    _stub_rag_deps,
    monkeypatch,
    caplog,
):
    """Unexpected cross-paper preparation errors are controlled and admin-visible."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    log_event_mock = AsyncMock()
    monkeypatch.setattr(
        "paper_ingestion.routers.rag.prepare_cross_paper_rag",
        AsyncMock(side_effect=RuntimeError("provider returned 500 with marker abc123")),
    )
    monkeypatch.setattr("paper_ingestion.routers.rag.log_event", log_event_mock, raising=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/ask",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )

    assert resp.status_code == 502
    assert resp.json()["detail"] == {
        "status": "degraded",
        "code": "rag_preparation_failed",
        "message": "RAG preparation failed.",
    }
    assert "abc123" not in resp.text
    assert "abc123" not in caplog.text
    log_event_mock.assert_awaited_once()
    kwargs = log_event_mock.await_args.kwargs
    assert kwargs["level"] == "error"
    assert kwargs["category"] == "error"
    assert kwargs["source"] == "rag"
    assert kwargs["message"] == "preparation_failed"
    assert kwargs["context"] == {
        "endpoint": "cross_paper",
        "phase": "preparation",
        "status_code": 502,
        "exception_class": "RuntimeError",
    }


# ---- ask_cross_paper_stream (stream): Qdrant error → degraded SSE frame ----


async def test_ask_cross_paper_stream_qdrant_error_returns_degraded_sse(
    _configure_api_key,
    _qdrant_error_env_cross,
    _stub_rag_deps,
):
    """ask_cross_paper_stream emits a degraded SSE error frame (retriable:true) on Qdrant error."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/ask/stream",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )
    assert resp.status_code == 200
    body = resp.text
    found_retriable = False
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
                if payload.get("type") == "error" and payload.get("retriable") is True:
                    found_retriable = True
                    break
            except json.JSONDecodeError:
                pass
    assert found_retriable, f"No retriable error SSE event found in: {body!r}"


# ---------------------------------------------------------------------------
# Regression: genuinely empty Qdrant hits still yield 422 (not 503)
# ---------------------------------------------------------------------------


async def test_ask_paper_empty_hits_still_returns_422(
    _configure_api_key,
    _patched_app_ownership,
    _empty_chunks_env,
    _stub_rag_deps,
):
    """ask_paper returns 422 (NoRelevantChunksError) for a genuinely empty hit list."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/papers/1/ask",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


@pytest.fixture()
def _cross_paper_empty_env(monkeypatch):
    """Patch prepare_cross_paper_rag to short-circuit with CrossPaperRagNoResults (healthy Qdrant, no hits)."""
    from paper_ingestion.rag.streaming import CrossPaperRagNoResults

    monkeypatch.setattr(
        "paper_ingestion.routers.rag.prepare_cross_paper_rag",
        AsyncMock(
            return_value=CrossPaperRagNoResults(
                answer="No relevant information found in the paper collection."
            )
        ),
    )


async def test_ask_cross_paper_empty_hits_returns_canned_answer(
    _configure_api_key,
    _cross_paper_empty_env,
    _stub_rag_deps,
):
    """ask_cross_paper returns the 200 canned answer (not 503/500) for genuinely empty hits on a healthy Qdrant."""
    from httpx import ASGITransport, AsyncClient

    from paper_ingestion.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/ask",
            json={"question": "What is this about?"},
            headers={"X-Api-Key": _configure_api_key},
        )
    assert resp.status_code == 200, (
        f"Expected 200 canned answer, got {resp.status_code}: {resp.text}"
    )
    assert "No relevant information" in resp.json()["answer"]
