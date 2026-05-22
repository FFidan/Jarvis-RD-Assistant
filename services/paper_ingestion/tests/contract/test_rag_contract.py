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

Wave 4.PI-rag additions:
  - test_cross_rag.py::test_ask_cross_paper_endpoint_structure collapsed here
    (W4.PI-rag; see test_ask_endpoint_cross_paper_real_db_structure)
  - test_filter_unread_starred_paper_remains_eligible (new): real schema exercise
    of Phase-A starred-boolean non-exclusion; strengthens the SQL-substring mock-unit
    in test_recommender.py::TestFilterUnread::test_starred_papers_remain_eligible_for_recommendation.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

# ---------------------------------------------------------------------------
# pi_test_client — shares the contract_conn transaction with the app
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
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
        "INSERT INTO users (email, role) VALUES ('owner@contract.example.com', 'user') RETURNING id"
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
async def test_ask_endpoint_cross_paper_real_db_structure(
    contract_conn, contract_two_users, pi_test_client
):
    """POST /api/ask uses real prep/DB and returns the RAG response envelope."""
    # Verified: services/paper_ingestion/paper_ingestion/routers/rag.py:384
    # /api/ask invokes real prepare_cross_paper_rag before scalar LLM generation.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-ask-01', 'arxiv', 'Ask Contract Paper', '{}', 'http://ask1')"
        " RETURNING id"
    )
    chunks = [
        {
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": "Transformers use self-attention over token representations.",
            "page_number": 1,
            "score": 0.9,
        }
    ]
    embedder = SimpleNamespace(
        search_chunks_global=AsyncMock(return_value=chunks),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )

    async def _stub_llm(http_client, messages, options, config):
        assert "Ask Contract Paper" in messages[0]["content"]
        return "Transformers use self-attention."

    pi_test_client.cookies.set("jarvis_session", contract_two_users.cookie_a)
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with patch(
            "paper_ingestion.routers.rag.request_chat_completion_content",
            side_effect=_stub_llm,
        ):
            resp = await pi_test_client.post(
                "/api/ask",
                json={"question": "How do transformers work?", "decompose": False},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True
        pi_test_client.cookies.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "Transformers use self-attention."
    assert isinstance(body["sources"], list)
    assert len(body["sources"]) == 1
    assert body["sources"][0]["paper_id"] == paper_id
    assert body["sources"][0]["paper_title"] == "Ask Contract Paper"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_ask_endpoint_cross_paper_llm_timeout_maps_504(
    contract_conn, contract_two_users, pi_test_client
):
    """POST /api/ask maps scalar LiteLLM timeout failures to HTTP 504."""
    # Verified: services/paper_ingestion/paper_ingestion/routers/rag.py:396
    # /api/ask calls request_chat_completion_content and maps timeout RuntimeError to 504.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-ask-timeout-01', 'arxiv', 'Timeout Contract Paper', '{}', 'http://timeout')"
        " RETURNING id"
    )
    chunks = [
        {
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": "Relevant finding.",
            "page_number": 1,
            "score": 0.9,
        }
    ]
    embedder = SimpleNamespace(
        search_chunks_global=AsyncMock(return_value=chunks),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )

    async def _stub_llm(http_client, messages, options, config):
        raise RuntimeError("LiteLLM chat request timed out") from httpx.ReadTimeout("timed out")

    pi_test_client.cookies.set("jarvis_session", contract_two_users.cookie_a)
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with patch(
            "paper_ingestion.routers.rag.request_chat_completion_content",
            side_effect=_stub_llm,
        ):
            resp = await pi_test_client.post(
                "/api/ask",
                json={"question": "How do transformers work?", "decompose": False},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True
        pi_test_client.cookies.clear()

    assert resp.status_code == 504, resp.text
    assert resp.json()["detail"] == "LLM request timed out"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_ask_endpoint_cross_paper_empty_visible_llm_maps_degraded_502(
    contract_conn, contract_two_users, pi_test_client
):
    """POST /api/ask maps empty visible scalar LLM content to explicit degraded 502."""
    # Verified: services/paper_ingestion/paper_ingestion/routers/rag.py:410
    # /api/ask maps EmptyVisibleLLMContentError to an explicit degraded 502 detail.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.auth import verify_api_key
    from jarvis_common.llm_client import EmptyVisibleLLMContentError
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-ask-empty-01', 'arxiv', 'Empty Visible Contract Paper', '{}', 'http://empty')"
        " RETURNING id"
    )
    chunks = [
        {
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": "Relevant finding.",
            "page_number": 1,
            "score": 0.9,
        }
    ]
    embedder = SimpleNamespace(
        search_chunks_global=AsyncMock(return_value=chunks),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )

    async def _stub_llm(http_client, messages, options, config):
        raise EmptyVisibleLLMContentError(
            "LiteLLM chat response contained no visible content after think-block stripping"
        )

    pi_test_client.cookies.set("jarvis_session", contract_two_users.cookie_a)
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with patch(
            "paper_ingestion.routers.rag.request_chat_completion_content",
            side_effect=_stub_llm,
        ):
            resp = await pi_test_client.post(
                "/api/ask",
                json={"question": "How do transformers work?", "decompose": False},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True
        pi_test_client.cookies.clear()

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == {
        "status": "degraded",
        "code": "llm_empty_visible_content",
        "message": "LLM response contained no visible answer.",
    }


# ---------------------------------------------------------------------------
# 4. _filter_unread: starred=TRUE papers remain eligible (real schema exercise)
#
# Strengthens: test_recommender.py::TestFilterUnread::test_starred_papers_remain_eligible_for_recommendation
#   (currently a SQL-substring mock-unit; real schema proves the predicate is absent).
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_filter_unread_starred_paper_remains_eligible(contract_conn):
    """_filter_unread must NOT exclude papers whose user_state has starred=TRUE.

    Phase-A: the starred boolean in paper_user_state drives _get_starred_ids
    (a signal source), but is NOT an exclusion predicate in _filter_unread.
    This contract test exercises the real SQL: a starred paper inserted with
    starred=TRUE must still appear in the _filter_unread result set.

    Strictly stronger than test_recommender.py::test_starred_papers_remain_eligible_for_recommendation
    (which only inspects the SQL text via a mock; this exercises the real schema).
    """
    from paper_ingestion.ingestion.recommender import _filter_unread

    # Seed user and paper
    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('starred-elig@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url)"
        " VALUES ('contract-starred-elig-01', 'arxiv', 'Starred Eligible Paper', '{}', 'http://se1')"
        " RETURNING id"
    )
    # Mark this paper as starred for the user (starred=TRUE must NOT gate _filter_unread).
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, starred) VALUES ($1, $2, TRUE)",
        paper_id,
        user_id,
    )

    result = await _filter_unread(contract_conn, [paper_id], user_id=user_id)
    assert paper_id in result, (
        "Paper with starred=TRUE must remain eligible for recommendation "
        "(starred is a signal source, not an exclusion predicate)"
    )


# ---------------------------------------------------------------------------
# A104. POST /api/papers/{paper_id}/ask — per-paper ask ownership + response shape
#
# Behavioral semantics: ownership is enforced via real DB before RAG prep;
# response carries answer + sources list with required keys.
# LLM + Qdrant + Ollama HTTP kept mocked (idiomatic external boundaries).
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a104_per_paper_ask_owner_gets_answer_shape(contract_conn, pi_test_client):
    """POST /api/papers/{paper_id}/ask uses real prep and returns the response envelope."""
    # Verified: services/paper_ingestion/paper_ingestion/routers/rag.py:211
    # Per-paper ask calls real prepare_single_paper_rag after ownership succeeds.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.auth import get_current_user_id, verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('a104-owner@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('contract-a104-01', 'arxiv', 'A104 Ask Contract Paper', '{}',
                   'http://a104', $1)
           RETURNING id""",
        user_id,
    )
    chunks = [
        {
            "content": "Key finding from the paper.",
            "page_number": 1,
            "score": 0.87,
        }
    ]
    embedder = SimpleNamespace(
        search_chunks_in_paper=AsyncMock(return_value=chunks),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )

    async def _stub_llm(http_client, messages, options, config):
        assert "A104 Ask Contract Paper" in messages[0]["content"]
        return "This paper is about contract tests."

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with patch(
            "paper_ingestion.routers.rag.request_chat_completion_content",
            side_effect=_stub_llm,
        ):
            resp = await pi_test_client.post(
                f"/api/papers/{paper_id}/ask",
                json={"question": "What is this paper about?"},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "This paper is about contract tests."
    assert isinstance(body["sources"], list)
    assert body["sources"][0]["paper_id"] == paper_id


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a104_per_paper_ask_llm_timeout_maps_504(contract_conn, pi_test_client):
    """POST /api/papers/{paper_id}/ask maps scalar LiteLLM timeouts to 504."""
    # Verified: services/paper_ingestion/paper_ingestion/routers/rag.py:231
    # Per-paper scalar timeout failures map to HTTP 504.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from jarvis_common.auth import get_current_user_id, verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('a104-timeout@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('contract-a104-timeout', 'arxiv', 'A104 Timeout Paper', '{}',
                   'http://a104-timeout', $1)
           RETURNING id""",
        user_id,
    )
    chunks = [{"content": "Finding.", "page_number": 1, "score": 0.87}]
    embedder = SimpleNamespace(
        search_chunks_in_paper=AsyncMock(return_value=chunks),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )

    async def _stub_llm(http_client, messages, options, config):
        raise RuntimeError("LiteLLM chat request timed out") from httpx.ReadTimeout("timed out")

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with patch(
            "paper_ingestion.routers.rag.request_chat_completion_content",
            side_effect=_stub_llm,
        ):
            resp = await pi_test_client.post(
                f"/api/papers/{paper_id}/ask",
                json={"question": "What is this paper about?"},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True

    assert resp.status_code == 504, resp.text
    assert resp.json()["detail"] == "LLM request timed out"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a104_per_paper_ask_empty_visible_maps_degraded_502(contract_conn, pi_test_client):
    """POST /api/papers/{paper_id}/ask maps empty visible scalar content to 502."""
    # Verified: services/paper_ingestion/paper_ingestion/routers/rag.py:233
    # Per-paper empty-visible scalar output maps to a degraded 502 detail object.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.auth import get_current_user_id, verify_api_key
    from jarvis_common.llm_client import EmptyVisibleLLMContentError
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('a104-empty@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('contract-a104-empty', 'arxiv', 'A104 Empty Paper', '{}',
                   'http://a104-empty', $1)
           RETURNING id""",
        user_id,
    )
    chunks = [{"content": "Finding.", "page_number": 1, "score": 0.87}]
    embedder = SimpleNamespace(
        search_chunks_in_paper=AsyncMock(return_value=chunks),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )

    async def _stub_llm(http_client, messages, options, config):
        raise EmptyVisibleLLMContentError("no visible content")

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with patch(
            "paper_ingestion.routers.rag.request_chat_completion_content",
            side_effect=_stub_llm,
        ):
            resp = await pi_test_client.post(
                f"/api/papers/{paper_id}/ask",
                json={"question": "What is this paper about?"},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "llm_empty_visible_content"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a104_per_paper_ask_non_owner_gets_403(contract_conn, pi_test_client):
    """POST /api/papers/{paper_id}/ask: non-owner receives 403.

    Real DB ownership check (assert_paper_ownership) fires before RAG prep.
    LLM + Qdrant mocks are irrelevant — the 403 is raised at the DB layer.
    """
    from unittest.mock import AsyncMock, MagicMock

    from jarvis_common.auth import get_current_user_id, verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    # Seed owner + paper.
    owner_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('a104-owner2@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('contract-a104-02', 'arxiv', 'A104 Non-Owner Paper', '{}',
                   'http://a104-2', $1)
           RETURNING id""",
        owner_id,
    )
    # Seed a second user who does NOT own the paper.
    intruder_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role)"
        " VALUES ('a104-intruder@contract.example.com', 'user') RETURNING id"
    )

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: intruder_id
    app.dependency_overrides[get_embedder] = lambda: AsyncMock()
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        resp = await pi_test_client.post(
            f"/api/papers/{paper_id}/ask",
            json={"question": "Snoop?"},
        )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True

    assert resp.status_code == 403, (
        f"Non-owner must receive 403; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# A105. POST /api/papers/{paper_id}/ask/stream — streaming SSE shape + ownership
#
# Verifies: StreamingResponse with media_type text/event-stream is returned for
# owner; ownership guard fires for non-owner BEFORE the stream is opened.
# LLM streaming + Qdrant + Ollama HTTP kept mocked (idiomatic external boundaries).
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a105_ask_stream_owner_gets_sse_response(contract_conn, pi_test_client):
    """POST /api/papers/{paper_id}/ask/stream: owner receives SSE content-type.

    The endpoint returns StreamingResponse(media_type='text/event-stream').
    DB assertion: paper exists + ownership check passes (real schema).
    Streaming LLM boundary stays mocked (stream_rag_events idiomatic mock).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.auth import get_current_user_id, verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('a105-owner@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('contract-a105-01', 'arxiv', 'A105 Stream Paper', '{}',
                   'http://a105', $1)
           RETURNING id""",
        user_id,
    )

    fake_sources = [{"paper_id": paper_id, "content": "chunk.", "page_number": 1, "score": 0.9}]
    fake_messages = [{"role": "user", "content": "stream question?"}]

    async def _stub_prepare(embedder, db_pool, paper_id_, body, http_client):
        return fake_messages, fake_sources

    async def _stub_stream(*args, **kwargs):
        # Minimal SSE: one token event + done terminator
        from jarvis_common.sse import SSE_DONE, sse_event

        yield sse_event({"type": "token", "content": "Hello."})
        yield sse_event({"type": "done", "full_answer": "Hello."})
        yield SSE_DONE

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_embedder] = lambda: AsyncMock()
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        with (
            patch(
                "paper_ingestion.routers.rag.prepare_single_paper_rag",
                side_effect=_stub_prepare,
            ),
            patch(
                "paper_ingestion.routers.rag.stream_rag_events",
                side_effect=_stub_stream,
            ),
        ):
            resp = await pi_test_client.post(
                f"/api/papers/{paper_id}/ask/stream",
                json={"question": "stream question?"},
            )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", ""), (
        "Streaming endpoint must return text/event-stream content-type"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a105_ask_stream_non_owner_gets_403(contract_conn, pi_test_client):
    """POST /api/papers/{paper_id}/ask/stream: non-owner receives 403.

    Ownership guard fires before stream is opened (real DB check).
    """
    from unittest.mock import AsyncMock, MagicMock

    from jarvis_common.auth import get_current_user_id, verify_api_key
    from paper_ingestion.deps import get_embedder, get_http_client, get_verifier
    from paper_ingestion.main import app

    owner_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('a105-owner2@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('contract-a105-02', 'arxiv', 'A105 Non-Owner Stream Paper', '{}',
                   'http://a105-2', $1)
           RETURNING id""",
        owner_id,
    )
    intruder_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role)"
        " VALUES ('a105-intruder@contract.example.com', 'user') RETURNING id"
    )

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: intruder_id
    app.dependency_overrides[get_embedder] = lambda: AsyncMock()
    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.state.limiter.enabled = False

    try:
        resp = await pi_test_client.post(
            f"/api/papers/{paper_id}/ask/stream",
            json={"question": "snoop?"},
        )
    finally:
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_embedder, None)
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.state.limiter.enabled = True

    assert resp.status_code == 403, (
        f"Non-owner must receive 403 on stream endpoint; got {resp.status_code}: {resp.text}"
    )
