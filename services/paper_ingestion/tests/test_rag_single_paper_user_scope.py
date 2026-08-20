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
from jarvis_common.testing import shelve_paper

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
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from paper_ingestion.deps import (
        get_db_pool,
        get_embedder,
        get_http_client,
        get_verifier,
        limiter,
    )
    from paper_ingestion.main import app
    from paper_ingestion.models.rag import AskResponse

    pool, _conn = _make_pool_and_conn()
    # pool=None: this test resolves the pool through the dependency override
    # only and leaves ``app.state`` untouched.
    with patch_pi_test_app(
        None,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            dependency_overrides={
                get_db_pool: lambda: pool,
                get_http_client: lambda: AsyncMock(spec=httpx.AsyncClient),
                get_embedder: lambda: AsyncMock(),
                get_verifier: lambda: AsyncMock(),
                verify_api_key: lambda: None,
            },
        ),
    ):
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

    async def _fetch(sql, *args):  # noqa: ARG001
        if "paper_chunks" in sql:
            return [{"paper_id": 42, "chunk_index": 0}]
        return [{"paper_id": 42}, {"paper_id": 99}]

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 42, "title": "T"})
    conn.fetch = AsyncMock(side_effect=_fetch)
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
    library_call = next(
        call for call in conn.fetch.await_args_list if "user_library" in call.args[0]
    )
    assert library_call.args[-1] == 7


# ---------------------------------------------------------------------------
# Persisted visibility for the single-paper loaders, exercised against real
# Postgres so the predicate itself is evaluated. A mock pool would return rows
# regardless of the SQL and prove nothing.
#
# A private paper reaches its owner (library membership) past the visibility
# gate but degrades to "not found" for a non-owner — the same persisted
# public-or-library policy cross-paper RAG enforces.
# ---------------------------------------------------------------------------


async def _seed_paper(
    conn,
    external_id: str,
    *,
    visibility_scope: str,
    discovered_by: int | None = None,
) -> int:
    return await conn.fetchval(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url,
               discovered_by, visibility_scope
           )
           VALUES ($1, 'arxiv', 'Shared Corpus Paper', ARRAY['Author'],
                   'https://shared.test/paper', $2, $3)
           RETURNING id""",
        external_id,
        discovered_by,
        visibility_scope,
    )


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_prepare_single_paper_rag_scopes_paper_to_caller(
    contract_two_users,
    contract_conn,
):
    """A non-owner's single-paper request degrades to PaperNotFoundError.

    The owner (who shelved the private paper) passes the visibility gate, so
    the empty-chunk branch — not PaperNotFoundError — surfaces. The owner
    control proves the predicate is discriminating, not a blanket deny.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.models import AskRequest
    from paper_ingestion.rag.exceptions import NoRelevantChunksError, PaperNotFoundError

    user_a = contract_two_users.user_a_id
    user_b = contract_two_users.user_b_id
    private_id = await _seed_paper(
        contract_conn,
        "single-private",
        visibility_scope="private",
        discovered_by=user_a,
    )
    await shelve_paper(contract_conn, user_a, private_id)

    pool = SharedConnPool(contract_conn)
    body = AskRequest(question="How does attention work?", max_chunks=3)

    with pytest.raises(PaperNotFoundError):
        await prepare_single_paper_rag(
            AsyncMock(),
            pool,
            paper_id=private_id,
            body=body,
            http_client=AsyncMock(),
            user_id=user_b,
        )

    owner_embedder = AsyncMock()
    owner_embedder.search_chunks_in_paper = AsyncMock(return_value=[])
    owner_embedder.rerank_chunks = AsyncMock(return_value=[])
    with pytest.raises(NoRelevantChunksError):
        await prepare_single_paper_rag(
            owner_embedder,
            pool,
            paper_id=private_id,
            body=body,
            http_client=AsyncMock(),
            user_id=user_a,
        )


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_load_paper_for_summary_scopes_paper_to_caller(
    contract_two_users,
    contract_conn,
):
    """_load_paper_for_summary gates the paper load by caller visibility.

    A non-owner degrades to PaperNotFoundError; the owner passes the gate and
    surfaces the downstream empty-chunks condition, proving the predicate admits
    the owner rather than denying everyone.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.exceptions import EmptyChunksError, PaperNotFoundError
    from paper_ingestion.services.summarization import _load_paper_for_summary

    user_a = contract_two_users.user_a_id
    user_b = contract_two_users.user_b_id
    private_id = await _seed_paper(
        contract_conn,
        "summary-private",
        visibility_scope="private",
        discovered_by=user_a,
    )
    await shelve_paper(contract_conn, user_a, private_id)

    pool = SharedConnPool(contract_conn)

    with pytest.raises(PaperNotFoundError):
        await _load_paper_for_summary(pool, paper_id=private_id, user_id=user_b, force=True)

    with pytest.raises(EmptyChunksError):
        await _load_paper_for_summary(pool, paper_id=private_id, user_id=user_a, force=True)


# ---------------------------------------------------------------------------
# Shared-corpus (public) branch of the reused predicate. The negative cases
# above exercise the caller-library disjunct; these pin the
# ``visibility_scope = 'public'`` disjunct so a refactor that dropped it —
# over-restricting the globally-shared corpus — is caught here rather than
# silently narrowing every caller to only their own library.
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_prepare_single_paper_rag_admits_public_shared_corpus_paper(
    contract_two_users,
    contract_conn,
):
    """A public shared-corpus paper stays visible to a non-discovering caller.

    A system paper (``discovered_by IS NULL``, public scope) that no user
    shelved must pass the visibility gate for any caller, so the downstream
    empty-chunk condition — not PaperNotFoundError — surfaces.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.models import AskRequest
    from paper_ingestion.rag.exceptions import NoRelevantChunksError

    non_discoverer = contract_two_users.user_b_id
    public_id = await _seed_paper(
        contract_conn,
        "single-public",
        visibility_scope="public",
        discovered_by=None,
    )

    pool = SharedConnPool(contract_conn)
    embedder = AsyncMock()
    embedder.search_chunks_in_paper = AsyncMock(return_value=[])
    embedder.rerank_chunks = AsyncMock(return_value=[])

    with pytest.raises(NoRelevantChunksError):
        await prepare_single_paper_rag(
            embedder,
            pool,
            paper_id=public_id,
            body=AskRequest(question="How does attention work?", max_chunks=3),
            http_client=AsyncMock(),
            user_id=non_discoverer,
        )


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_load_paper_for_summary_admits_public_shared_corpus_paper(
    contract_two_users,
    contract_conn,
):
    """_load_paper_for_summary keeps a public shared-corpus paper visible.

    The public disjunct must admit a non-discovering caller: a system paper
    (``discovered_by IS NULL``, public scope) reaches the downstream
    empty-chunks condition rather than PaperNotFoundError.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.exceptions import EmptyChunksError
    from paper_ingestion.services.summarization import _load_paper_for_summary

    non_discoverer = contract_two_users.user_b_id
    public_id = await _seed_paper(
        contract_conn,
        "summary-public",
        visibility_scope="public",
        discovered_by=None,
    )

    pool = SharedConnPool(contract_conn)

    with pytest.raises(EmptyChunksError):
        await _load_paper_for_summary(pool, paper_id=public_id, user_id=non_discoverer, force=True)
