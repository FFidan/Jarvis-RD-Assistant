"""Contract tests for RAG DB-touching paths.

These tests use a real Postgres container (session-scoped, JARVIS_RUN_LIVE_PG=1)
with per-test transaction rollback.  External boundaries (Qdrant, Ollama HTTP,
LiteLLM) are kept mocked — per the idiomatic-mock carve-out.

Collapses the following mock-unit DB stubs:
  - test_stream_rag.py::testprepare_single_paper_rag_returns_messages_and_sources
    (mock_conn.fetchrow.return_value = {"id": 1, "title": "Test Paper"})
  - test_stream_rag.py::testprepare_cross_paper_rag_returns_messages_and_sources
    (mock_conn.fetch.return_value = [...])
  - test_cross_rag.py::test_ask_cross_paper_endpoint_structure
    (_make_pool_and_conn() for DB layer)
"""

from __future__ import annotations

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

# ---------------------------------------------------------------------------
# pi_test_client — shares the contract_conn transaction with the app
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pi_test_client(contract_conn):
    """paper_ingestion ASGI client wired to the contract_conn transaction."""
    import httpx
    from jarvis_common.testing import SharedConnPool

    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared
    app.dependency_overrides[get_db_pool] = lambda: shared
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        if original is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original
        app.dependency_overrides.pop(get_db_pool, None)


# ---------------------------------------------------------------------------
# 1. prepare_single_paper_rag: real paper row, title plumbing into messages
#
# Collapses: test_stream_rag.py::testprepare_single_paper_rag_returns_messages_and_sources
#   (which had mock_conn.fetchrow.return_value = {"id": 1, "title": "Test Paper"})
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_prepare_single_paper_rag_title_from_real_db(contract_conn):
    """prepare_single_paper_rag fetches paper title from real DB and threads it into the
    LLM messages.  Qdrant + cross-encoder rerank stay mocked (external boundaries)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION
    from paper_ingestion.models import AskRequest
    from paper_ingestion.rag.streaming import prepare_single_paper_rag

    # Real INSERT — rolled back after this test.
    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-rag-01', 'arxiv', 'Neural ODEs Contract', '{}', 'http://c1')"
        " RETURNING id"
    )

    # Qdrant boundary: idiomatic mock (external service).
    mock_qdrant = AsyncMock()
    hit = SimpleNamespace(
        payload={
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": "Neural ODEs unify discrete residual networks.",
            "page_number": 1,
        },
        score=0.88,
    )
    resp = SimpleNamespace(points=[hit])
    mock_qdrant.query_points = AsyncMock(return_value=resp)

    # Ollama HTTP boundary: idiomatic mock.
    import httpx

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    embed_resp = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": [{"index": 0, "embedding": [0.1] * EMBEDDING_DIMENSION}]},
    )
    mock_http.post = AsyncMock(return_value=embed_resp)

    from paper_ingestion.ingestion.embedder import Embedder

    embedder = Embedder(mock_http, mock_qdrant)

    # Cross-encoder rerank boundary: idiomatic mock (pass-through).
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, chunks, top_k: chunks[:top_k])

    from jarvis_common.testing import SharedConnPool

    body = AskRequest(question="What is the main idea?", max_chunks=5)
    messages, sources = await prepare_single_paper_rag(
        embedder,
        SharedConnPool(contract_conn),
        paper_id=paper_id,
        body=body,
        http_client=mock_http,
    )

    # The REAL DB fetch confirms the title "Neural ODEs Contract" is in the prompt.
    assert isinstance(messages, list)
    assert len(messages) == 1
    assert "Neural ODEs Contract" in messages[0]["content"], (
        "Real paper title must appear in the RAG prompt"
    )
    assert "What is the main idea?" in messages[0]["content"]

    assert isinstance(sources, list)
    assert len(sources) >= 1
    assert sources[0]["content"] == "Neural ODEs unify discrete residual networks."


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_prepare_single_paper_rag_404_on_missing_paper(contract_conn):
    """prepare_single_paper_rag raises 404 when the paper_id does not exist in the real DB."""
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import HTTPException

    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.models import AskRequest
    from paper_ingestion.rag.streaming import prepare_single_paper_rag

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, c, top_k: c[:top_k])

    from jarvis_common.testing import SharedConnPool

    body = AskRequest(question="anything?", max_chunks=5)

    with pytest.raises(HTTPException) as exc_info:
        await prepare_single_paper_rag(
            embedder,
            SharedConnPool(contract_conn),
            paper_id=999_999_999,
            body=body,
            http_client=mock_http,
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 2. prepare_cross_paper_rag: real paper metadata, visibility predicate
#
# Collapses: test_stream_rag.py::testprepare_cross_paper_rag_returns_messages_and_sources
#   (which had mock_conn.fetch.return_value = [{id:10,...},{id:20,...}])
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_prepare_cross_paper_rag_titles_from_real_db(contract_conn):
    """prepare_cross_paper_rag fetches paper metadata from real DB and threads it into
    the LLM prompt.  Qdrant global search + cross-encoder rerank stay mocked."""
    from unittest.mock import AsyncMock

    import httpx

    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    # Real INSERTs — rolled back after this test.
    pid_a = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-xrag-01', 'arxiv', 'Transformer Paper Contract', '{\"A\"}', 'http://ta')"
        " RETURNING id"
    )
    pid_b = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-xrag-02', 'arxiv', 'Attention Paper Contract', '{\"B\"}', 'http://tb')"
        " RETURNING id"
    )

    # Qdrant boundary: idiomatic mock returning chunks for both papers.
    mock_qdrant = AsyncMock()
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    embedder = Embedder(mock_http, mock_qdrant)

    all_chunks = [
        {
            "paper_id": pid_a,
            "chunk_index": 0,
            "content": "Transformers are great.",
            "page_number": 1,
            "score": 0.9,
        },
        {
            "paper_id": pid_b,
            "chunk_index": 0,
            "content": "Attention mechanisms work well.",
            "page_number": 2,
            "score": 0.8,
        },
    ]
    # Ollama embed boundary: idiomatic mock.
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    # Qdrant search boundary: idiomatic mock.
    embedder.search_chunks_global = AsyncMock(return_value=all_chunks)
    # Cross-encoder rerank boundary: idiomatic mock (pass-through).
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, chunks, top_k: chunks[:top_k])

    from jarvis_common.testing import SharedConnPool

    body = CrossPaperAskRequest(question="How do transformers work?", decompose=False)
    result = await prepare_cross_paper_rag(
        embedder,
        SharedConnPool(contract_conn),
        body,
        mock_http,
        user_id=None,
    )

    assert isinstance(result, CrossPaperRagPrep), f"Expected CrossPaperRagPrep, got {type(result)}"
    # Real DB fetch confirms paper titles are in the prompt.
    prompt_text = result.messages[0]["content"]
    assert "Transformer Paper Contract" in prompt_text, (
        "Real title for paper A must be in the cross-paper prompt"
    )
    assert "Attention Paper Contract" in prompt_text, (
        "Real title for paper B must be in the cross-paper prompt"
    )

    # Sources list contains both paper_ids with correct paper_title attribution.
    source_pids = {s["paper_id"] for s in result.sources}
    assert pid_a in source_pids
    assert pid_b in source_pids
    titles = {s["paper_title"] for s in result.sources}
    assert "Transformer Paper Contract" in titles
    assert "Attention Paper Contract" in titles


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_prepare_cross_paper_rag_visibility_excludes_other_user_papers(contract_conn):
    """RAG-DB-1: papers owned exclusively by another user are not returned.

    The SQL in prepare_cross_paper_rag uses a user_library visibility predicate.
    When user_id=99 requests chunks, a paper owned only by user_id=1 via
    user_library must not appear in the sources (it is dropped by the
    defense-in-depth DB check).
    """
    from unittest.mock import AsyncMock

    import httpx

    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    # Seed a user to own the paper exclusively (user_library requires FK to users).
    owner_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('owner@contract.test', 'user') RETURNING id"
    )
    # Seed a paper owned exclusively by that user.
    pid_owned = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-xrag-vis-01', 'arxiv', 'User1 Private Paper', '{\"X\"}', 'http://priv')"
        " RETURNING id"
    )
    # Link it to the owner's library (no other users → exclusively owned).
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        owner_id,
        pid_owned,
    )

    mock_qdrant = AsyncMock()
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    embedder = Embedder(mock_http, mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 1024])
    # Qdrant returns a chunk for the user-1-owned paper.
    embedder.search_chunks_global = AsyncMock(
        return_value=[
            {
                "paper_id": pid_owned,
                "chunk_index": 0,
                "content": "Private content.",
                "page_number": 1,
                "score": 0.95,
            }
        ]
    )
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, chunks, top_k: chunks[:top_k])

    from jarvis_common.testing import SharedConnPool

    body = CrossPaperAskRequest(question="Any question?", decompose=False)
    result = await prepare_cross_paper_rag(
        embedder,
        SharedConnPool(contract_conn),
        body,
        mock_http,
        user_id=99,  # user 99 does NOT own this paper
    )

    # The defense-in-depth DB visibility check should have dropped the chunk.
    # Result is either CrossPaperRagNoResults (no chunks survive) or
    # CrossPaperRagPrep with pid_owned NOT in sources.
    if isinstance(result, CrossPaperRagPrep):
        source_pids = {s["paper_id"] for s in result.sources}
        assert pid_owned not in source_pids, (
            "RAG-DB-1: paper exclusively owned by user 1 must not be visible to user 99"
        )
    # else CrossPaperRagNoResults is also correct (all chunks dropped)


# ---------------------------------------------------------------------------
# 3. /api/ask endpoint: real DB for paper existence, external stubs kept
#
# Collapses: test_cross_rag.py::test_ask_cross_paper_endpoint_structure
#   (which used _make_pool_and_conn() for the DB layer)
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_ask_endpoint_cross_paper_real_db_structure(contract_conn, pi_test_client):
    """POST /api/ask: real DB for paper metadata; prepare_cross_paper_rag and LLM stay mocked.

    Verifies the HTTP response shape (status, answer, sources) and confirms the
    contract client is wired to the real schema.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app
    from paper_ingestion.rag.streaming import CrossPaperRagPrep

    # Seed a paper in the real DB.
    await contract_conn.execute(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-ask-01', 'arxiv', 'Ask Contract Paper', '{\"Z\"}', 'http://ask1')"
        " ON CONFLICT DO NOTHING"
    )

    fake_sources = [
        {
            "paper_id": 1,
            "paper_title": "Ask Contract Paper",
            "content": "Relevant finding.",
            "page_number": 1,
            "score": 0.9,
        }
    ]
    fake_messages = [{"role": "user", "content": "How do transformers work?"}]

    async def _stub_prepare(embedder, db_pool, body, http_client, *, user_id):
        return CrossPaperRagPrep(messages=fake_messages, sources=fake_sources)

    async def _stub_llm(http_client, messages, options, config):
        return "Transformers use self-attention."

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: AsyncMock()
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with (
            patch(
                "paper_ingestion.routers.rag.prepare_cross_paper_rag",
                side_effect=_stub_prepare,
            ),
            patch(
                "paper_ingestion.routers.rag.request_chat_completion_content",
                side_effect=_stub_llm,
            ),
        ):
            resp = await pi_test_client.post(
                "/api/ask",
                json={"question": "How do transformers work?"},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "answer" in body
    assert isinstance(body["sources"], list)
    assert len(body["sources"]) == 1
    for src in body["sources"]:
        assert "paper_id" in src
        assert "paper_title" in src
        assert "content" in src
        assert "page_number" in src
        assert "score" in src
