"""``ask_paper`` forwards ``user_id`` to ``prepare_single_paper_rag``.

Boundary-adapter test: patches the prepared-RAG helper to capture kwargs, then
hits the ``/api/papers/{id}/ask`` route via the in-process ASGI transport.  The
test asserts only that ``user_id`` is forwarded as a kwarg; the broader RAG
behaviour is covered by the existing ``test_rag_authorization`` /
``test_rag_contract`` suites.

Also covers the M7 defense-in-depth Qdrant filter composition of
``search_chunks_in_paper``: the user scope must be NESTED as one element of
the outer ``must`` list (a sub-Filter).  Flat-merging its ``should`` branches
beside the ``must`` list would make them advisory (scoring-only) in real
Qdrant, so the composition shape is asserted directly at the
Qdrant boundary.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from paper_ingestion.rag.streaming import prepare_single_paper_rag
from tests.conftest import _make_pool_and_conn

_VISIBILITY_GENERATION = "a" * 32


async def _current_visibility_generation() -> str:
    """Return the generation expected in test Qdrant filters."""
    return _VISIBILITY_GENERATION


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


# ---------------------------------------------------------------------------
# M7: search_chunks_in_paper — Qdrant filter composition at the boundary
# ---------------------------------------------------------------------------


def _make_embedder_with_captured_qdrant():
    """Real Embedder with a mocked Qdrant boundary returning zero points."""
    from types import SimpleNamespace

    from paper_ingestion.ingestion.embedder import Embedder

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points = AsyncMock(return_value=SimpleNamespace(points=[]))
    embedder = Embedder(
        mock_http,
        mock_qdrant,
        visibility_generation_provider=_current_visibility_generation,
    )
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 8])  # type: ignore[method-assign]
    return embedder, mock_qdrant


def _assert_persisted_scope_filter(scope_filter, private_paper_ids: set[int]) -> None:
    """Assert generation plus public-or-caller-private filter composition."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    assert isinstance(scope_filter, Filter)
    assert scope_filter.must is not None and len(scope_filter.must) == 2
    generation_condition, access_filter = scope_filter.must
    assert isinstance(generation_condition, FieldCondition)
    assert generation_condition.key == "visibility_generation"
    assert generation_condition.match == MatchValue(value=_VISIBILITY_GENERATION)

    assert isinstance(access_filter, Filter)
    access_branches = access_filter.should or []
    assert any(
        isinstance(branch, FieldCondition)
        and branch.key == "visibility_scope"
        and branch.match == MatchValue(value="public")
        for branch in access_branches
    )
    private_filters = [branch for branch in access_branches if isinstance(branch, Filter)]
    assert len(private_filters) == 1
    private_conditions = private_filters[0].must or []
    assert any(
        isinstance(condition, FieldCondition)
        and condition.key == "visibility_scope"
        and condition.match == MatchValue(value="private")
        for condition in private_conditions
    )
    paper_conditions = [
        condition
        for condition in private_conditions
        if isinstance(condition, FieldCondition)
        and condition.key == "paper_id"
        and isinstance(condition.match, MatchAny)
    ]
    assert len(paper_conditions) == 1
    assert set(paper_conditions[0].match.any) == private_paper_ids


@pytest.mark.asyncio
async def test_search_chunks_in_paper_nests_visibility_scope_as_must_subfilter() -> None:
    """Persisted visibility must be one restrictive nested filter.

    Regression guard: if a refactor flat-merges the scope's
    ``should`` branches beside the outer ``must`` list, the outer ``should``
    assertion fails (advisory-only in real Qdrant = silent cross-tenant leak).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    embedder, mock_qdrant = _make_embedder_with_captured_qdrant()

    await embedder.search_chunks_in_paper(
        query_text="q",
        paper_id=42,
        user_id=7,
        library_paper_ids=[42, 99],
    )

    assert mock_qdrant.query_points.await_count == 1
    query_filter = mock_qdrant.query_points.await_args.kwargs["query_filter"]

    # M6 guard: nothing may sit in the outer `should` slot.
    assert query_filter.should is None, (
        "user-scope branches must NOT be flat-merged as outer `should` — beside a "
        "`must` list they are advisory (scoring-only) in Qdrant, not restrictive"
    )

    assert query_filter.must is not None and len(query_filter.must) == 2, (
        f"outer must = [paper_id cond, nested scope Filter]; got {query_filter.must!r}"
    )
    paper_cond, nested = query_filter.must
    assert isinstance(paper_cond, FieldCondition) and paper_cond.key == "paper_id"
    assert paper_cond.match == MatchValue(value=42)

    assert isinstance(nested, Filter), "user scope must be a nested sub-Filter"
    _assert_persisted_scope_filter(nested, {42, 99})


@pytest.mark.asyncio
async def test_search_chunks_in_paper_default_none_filter_unchanged() -> None:
    """Without user params the filter stays paper-id-only (extraction/system path).

    Guards the RAG-DB-1 regression class for system-context callers
    (``extraction/core.py`` calls without user scope): default-None must
    produce the exact pre-M7 filter shape.
    """
    from qdrant_client.models import FieldCondition, MatchValue

    embedder, mock_qdrant = _make_embedder_with_captured_qdrant()

    await embedder.search_chunks_in_paper(query_text="q", paper_id=42)

    assert mock_qdrant.query_points.await_count == 1
    query_filter = mock_qdrant.query_points.await_args.kwargs["query_filter"]
    assert query_filter.should is None
    assert query_filter.must_not is None
    assert query_filter.must is not None and len(query_filter.must) == 1
    (paper_cond,) = query_filter.must
    assert isinstance(paper_cond, FieldCondition) and paper_cond.key == "paper_id"
    assert paper_cond.match == MatchValue(value=42)


@pytest.mark.asyncio
async def test_search_similar_nests_visibility_scope_as_must_subfilter() -> None:
    """Persisted visibility must be one restrictive nested filter.

    Regression guard: ``search_similar`` previously flat-merged the
    scope's ``should`` branches beside ``must_not``, where they are advisory
    (scoring-only) in real Qdrant — a silent cross-tenant leak.  The paper
    exclusion stays in ``must_not``; the scope must be restrictive (``must``).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    embedder, mock_qdrant = _make_embedder_with_captured_qdrant()

    await embedder.search_similar(
        query_text="q",
        paper_id_filter=42,
        user_id=7,
        library_paper_ids=[42, 99],
    )

    assert mock_qdrant.query_points.await_count == 1
    query_filter = mock_qdrant.query_points.await_args.kwargs["query_filter"]

    # M6 guard: nothing may sit in the outer `should` slot.
    assert query_filter.should is None, (
        "user-scope branches must NOT be flat-merged as outer `should` — beside a "
        "`must_not` list they are advisory (scoring-only) in Qdrant, not restrictive"
    )

    # Paper exclusion stays restrictive in must_not.
    assert query_filter.must_not is not None and len(query_filter.must_not) == 1
    (excluded,) = query_filter.must_not
    assert isinstance(excluded, FieldCondition) and excluded.key == "paper_id"
    assert excluded.match == MatchValue(value=42)

    # User scope is the sole nested sub-Filter in the restrictive `must` list.
    assert query_filter.must is not None and len(query_filter.must) == 1, (
        f"outer must = [nested scope Filter]; got {query_filter.must!r}"
    )
    (nested,) = query_filter.must
    assert isinstance(nested, Filter), "user scope must be a nested sub-Filter"
    _assert_persisted_scope_filter(nested, {42, 99})


@pytest.mark.asyncio
async def test_search_similar_default_none_filter_unscoped() -> None:
    """Without user params the filter stays paper-exclusion-only (system path).

    ``user_id is None`` must preserve the pre-fix unscoped behaviour: no scope
    is AND-combined, only the optional paper exclusion lands in ``must_not``.
    """
    from qdrant_client.models import FieldCondition, MatchValue

    embedder, mock_qdrant = _make_embedder_with_captured_qdrant()

    await embedder.search_similar(query_text="q", paper_id_filter=42)

    assert mock_qdrant.query_points.await_count == 1
    query_filter = mock_qdrant.query_points.await_args.kwargs["query_filter"]
    assert query_filter.should is None
    assert query_filter.must is None
    assert query_filter.must_not is not None and len(query_filter.must_not) == 1
    (excluded,) = query_filter.must_not
    assert isinstance(excluded, FieldCondition) and excluded.key == "paper_id"
    assert excluded.match == MatchValue(value=42)


@pytest.mark.asyncio
async def test_prepare_single_paper_rag_threads_user_scope_to_search() -> None:
    """prepare_single_paper_rag fetches the caller's library and threads scope down."""
    from unittest.mock import MagicMock

    from paper_ingestion.models import AskRequest

    chunks = [{"content": "text", "page_number": 1, "score": 0.9, "chunk_index": 0}]
    embedder = AsyncMock()
    embedder.search_chunks_in_paper = AsyncMock(return_value=chunks)
    embedder.rerank_chunks = AsyncMock(return_value=chunks)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 42, "title": "T"})
    conn.fetch = AsyncMock(return_value=[{"paper_id": 42}, {"paper_id": 99}])
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await prepare_single_paper_rag(
        embedder,
        pool,
        paper_id=42,
        body=AskRequest(question="q?", max_chunks=3),
        http_client=AsyncMock(spec=httpx.AsyncClient),
        user_id=7,
    )

    kwargs = embedder.search_chunks_in_paper.await_args.kwargs
    assert kwargs.get("user_id") == 7, "user_id must be threaded into the Qdrant search"
    assert kwargs.get("library_paper_ids") == [42, 99], (
        "the caller's own user_library paper ids must be threaded down (PI-RAG-001)"
    )
    # The library lookup must be keyed on the CALLER's id (real-SQL coverage of
    # the user_library query lives in the sidecar contract test).
    assert conn.fetch.await_args.args[-1] == 7
