"""Discovery endpoint contract tests.

Covers the DB-layer behaviors in POST /api/discover and GET /api/similar/{paper_id}:
  - Ownership enforcement (assert_paper_ownership) for seed papers
  - 404 for missing seed paper IDs
  - Cross-user IDOR guard (user B cannot discover from user A's private paper)

Idiomatic-mock carve-out (KEEP):
  - embedder.discover_from_seeds — Qdrant RecommendQuery boundary (kept mocked)
  - embedder.search_similar — Qdrant query boundary (kept mocked)
  - app.state.http_client = MagicMock() (outbound HTTP boundary)

Contract test targets:
  - Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:125-207
    (POST /api/discover — discover_papers)
  - Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:31-117
    (GET /api/similar/{paper_id} — find_similar_papers)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool

from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    patch_pi_test_app,
)
from jarvis_common.testing_contract_apps import (
    make_contract_client as _client,
)
from paper_ingestion.ingestion.search import EmbeddingSearchMixin

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# App + client fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app(contract_conn):
    """paper_ingestion app wired to the contract connection, rate limiter off."""
    from unittest.mock import AsyncMock, MagicMock

    from paper_ingestion.deps import get_db_pool, get_embedder, limiter
    from paper_ingestion.main import app

    # Idiomatic mock for embedder (Qdrant/Ollama boundary)
    mock_embedder = MagicMock()
    mock_embedder.qdrant = MagicMock()  # non-None so 503 is not raised
    mock_embedder.discover_from_seeds = AsyncMock(return_value=[])
    mock_embedder.search_similar = AsyncMock(return_value=[])
    mock_embedder.hybrid_search = AsyncMock(return_value=[])

    shared = SharedConnPool(contract_conn)
    with patch_pi_test_app(
        shared,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            mock_http_client=True,
            state_overrides={"embedder": mock_embedder},
            dependency_overrides={get_embedder: lambda: mock_embedder},
        ),
    ) as wired_app:
        yield wired_app


# ---------------------------------------------------------------------------
# §A-DISC-01 — POST /api/discover: 404 for non-existent seed paper IDs
# Verified: discovery.py:152-164 (missing paper_ids → 404)
# ---------------------------------------------------------------------------


async def test_discover_404_for_nonexistent_seed_paper(
    contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/discover returns 404 when seed paper_ids do not exist in DB.

    The ownership check (assert_paper_ownership) or the existence check
    must raise 404 before reaching the Qdrant discovery call.
    """
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": [999999999], "limit": 5},
        )

    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 for non-existent seed paper; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-02 — POST /api/discover: IDOR — user B cannot discover from user A's paper
# Verified: discovery.py:151-164 (assert_paper_ownership for each seed paper_id)
# ---------------------------------------------------------------------------


async def test_discover_idor_user_b_cannot_use_user_a_seed(
    contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/discover: user B cannot seed from a paper owned only by user A.

    assert_paper_ownership raises 403 when the caller is not the paper's owner
    (discovered_by != caller user_id).
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": [paper_id_a], "limit": 5},
        )

    assert resp.status_code in (403, 404), (
        f"IDOR: user B must get 403/404 when seeding from user A's paper {paper_id_a}; "
        f"got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-03 — POST /api/discover: owner gets valid response (empty Qdrant mock)
# Verified: discovery.py:125-207 (discover_papers full path)
# ---------------------------------------------------------------------------


async def test_discover_owner_gets_200_with_empty_results(
    contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/discover returns 200 with empty list when Qdrant returns no results.

    Confirms the ownership check passes for the paper's owner and the DB metadata
    enrichment path is reached without error (even when Qdrant mock returns []).
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": [paper_id_a], "limit": 5},
        )

    assert resp.status_code == 200, (
        f"Owner expected 200 from /api/discover; got {resp.status_code}: {resp.text}"
    )
    assert isinstance(resp.json(), list), "Response must be a list"


# ---------------------------------------------------------------------------
# §A-DISC-04 — POST /api/discover: paper_ids > schema max returns 422
# Verified: models/papers.py:341-344 (DiscoverRequest max_length=10)
# ---------------------------------------------------------------------------


async def test_discover_422_for_too_many_seed_ids(contract_two_users, _pi_app, _configure_api_key):
    """POST /api/discover returns 422 when paper_ids exceeds the request schema limit."""
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/discover",
            json={"paper_ids": list(range(1, 12)), "limit": 5},
        )

    assert resp.status_code == 422, (
        f"Expected 422 for request-schema seed ID overflow; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-05 — GET /api/similar/{paper_id}: IDOR — user B cannot query user A's paper
# Verified: discovery.py:57-58 (current_user_id_strict + assert_paper_ownership)
# ---------------------------------------------------------------------------


async def test_similar_idor_user_b_cannot_query_user_a_paper(
    contract_two_users, _pi_app, _configure_api_key
):
    """GET /api/similar/{paper_id}: user B cannot find papers similar to user A's paper.

    assert_paper_ownership raises 403 before any Qdrant call.
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/similar/{paper_id_a}")

    assert resp.status_code in (403, 404), (
        f"IDOR: user B must get 403/404 for GET /api/similar/{paper_id_a}; "
        f"got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-06 — GET /api/similar/{paper_id}: 404 for non-existent paper
# Verified: discovery.py:57-63 (assert_paper_ownership → 404 when paper absent)
# ---------------------------------------------------------------------------


async def test_similar_404_for_nonexistent_paper(contract_two_users, _pi_app, _configure_api_key):
    """GET /api/similar/{paper_id} returns 403 or 404 for a non-existent paper_id."""
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/similar/999999999")

    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 for non-existent paper; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-DISC-07 — GET /api/similar/{paper_id}: owner gets 200 with empty results
# Verified: discovery.py:31-117 (find_similar_papers full path)
# ---------------------------------------------------------------------------


async def test_similar_owner_gets_200_with_empty_results(
    contract_two_users, _pi_app, _configure_api_key
):
    """GET /api/similar/{paper_id} returns 200 list for the paper's owner.

    Qdrant mock returns [] → enriched list is empty; endpoint still returns 200 [].
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/similar/{paper_id_a}")

    assert resp.status_code == 200, (
        f"Owner expected 200 from /api/similar/{paper_id_a}; got {resp.status_code}: {resp.text}"
    )
    assert isinstance(resp.json(), list), "Response must be a list"


# ---------------------------------------------------------------------------
# Snippet backing — both endpoints serve the vector payload's own copy of an
# excerpt, so a result only stands while the owning paper still stores that
# chunk.
# ---------------------------------------------------------------------------

_SUPERSEDED_TEXT = "ZZZ-DISCOVERY-SUPERSEDED excerpt from content no longer stored"
_STORED_TEXT = "ZZZ-DISCOVERY-STORED excerpt from content still stored"


async def _seed_paper_with_chunks(conn, owner_id: int, tag: str, chunk_indexes: list[int]) -> int:
    """Insert a paper plus one stored chunk row per index, with no library row.

    The paper keeps the default private visibility scope, so nobody can see it
    until a library row grants access.

    Verified: db/init.sql:701-713 — paper_chunks (chunk_index NOT NULL, content NOT NULL).
    Verified: db/migrations/0106_paper_visibility_scope.sql:7 — visibility_scope
      defaults to 'private'.
    """
    paper_id = await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', $2, ARRAY['A. Author'], 'https://example.test/snippet', $3)
           RETURNING id""",
        f"disc-snippet-{tag}",
        f"discovery snippet paper {tag}",
        owner_id,
    )
    for chunk_index in chunk_indexes:
        await conn.execute(
            """INSERT INTO paper_chunks (paper_id, chunk_index, content)
               VALUES ($1, $2, 'stored chunk text')""",
            paper_id,
            chunk_index,
        )
    return int(paper_id)


async def _seed_library_paper(conn, user_id: int, tag: str, chunk_indexes: list[int]) -> int:
    """Insert a paper in the user's library plus one stored chunk row per index."""
    paper_id = await _seed_paper_with_chunks(conn, user_id, tag, chunk_indexes)
    await conn.execute(
        """INSERT INTO user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        user_id,
        paper_id,
    )
    return paper_id


async def _run_both_endpoints(app, cookie, seed_paper_id: int):
    """Call GET /api/similar/{id} and POST /api/discover with the same stub hits."""
    async with _client(app, cookie) as c:
        similar = await c.get(f"/api/similar/{seed_paper_id}")
        discover = await c.post(
            "/api/discover",
            json={"paper_ids": [seed_paper_id], "limit": 5},
        )
    return {"GET /api/similar": similar, "POST /api/discover": discover}


# ---------------------------------------------------------------------------
# §A-DISC-08 — neither endpoint serves an excerpt with no stored chunk record
# Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:120,212
#   (drop_chunks_without_stored_rows gates both enrichment loops)
# Verified: services/paper_ingestion/paper_ingestion/queries/chunk_liveness.py:71-79
#   (a chunk carrying no chunk_index matches nothing and is dropped)
# ---------------------------------------------------------------------------


async def test_discovery_omits_snippets_without_a_stored_chunk_row(
    contract_two_users, _pi_app, _configure_api_key, contract_conn
):
    """A hit is dropped when its paper no longer stores that chunk.

    Two shapes must drop: a hit whose (paper_id, chunk_index) has no
    paper_chunks row, and a hit carrying no chunk_index at all.
    """
    user_a_id = contract_two_users.user_a_id
    unstored_id = await _seed_library_paper(contract_conn, user_a_id, "unstored", [])
    no_index_id = await _seed_library_paper(contract_conn, user_a_id, "no-index", [0])

    hits = [
        {"paper_id": unstored_id, "chunk_index": 0, "content": _SUPERSEDED_TEXT, "score": 0.9},
        {"paper_id": no_index_id, "content": _SUPERSEDED_TEXT, "score": 0.8},
    ]
    _pi_app.state.embedder.search_similar = AsyncMock(return_value=hits)
    _pi_app.state.embedder.discover_from_seeds = AsyncMock(return_value=hits)

    responses = await _run_both_endpoints(
        _pi_app, contract_two_users.cookie_a, contract_two_users.paper_id_a
    )

    for label, resp in responses.items():
        assert resp.status_code == 200, (
            f"{label} expected 200; got {resp.status_code}: {resp.text[:300]}"
        )
        assert _SUPERSEDED_TEXT not in resp.text, (
            f"{label} served an excerpt with no stored chunk record: {resp.text[:300]}"
        )
        assert resp.json() == [], f"{label} must drop unbacked hits entirely; got {resp.json()}"


# ---------------------------------------------------------------------------
# §A-DISC-09 — a stored chunk record still yields its snippet from both endpoints
# Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:112,206
#   (read_stored_chunk_keys reads the live keys on the connection already held)
# ---------------------------------------------------------------------------


async def test_discovery_serves_snippets_backed_by_a_stored_chunk_row(
    contract_two_users, _pi_app, _configure_api_key, contract_conn
):
    """A hit whose (paper_id, chunk_index) is stored keeps its matching_snippet."""
    user_a_id = contract_two_users.user_a_id
    stored_id = await _seed_library_paper(contract_conn, user_a_id, "stored", [0])

    hits = [{"paper_id": stored_id, "chunk_index": 0, "content": _STORED_TEXT, "score": 0.9}]
    _pi_app.state.embedder.search_similar = AsyncMock(return_value=hits)
    _pi_app.state.embedder.discover_from_seeds = AsyncMock(return_value=hits)

    responses = await _run_both_endpoints(
        _pi_app, contract_two_users.cookie_a, contract_two_users.paper_id_a
    )

    for label, resp in responses.items():
        assert resp.status_code == 200, (
            f"{label} expected 200; got {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
        assert [item["paper_id"] for item in body] == [stored_id], (
            f"{label} must return the backed paper; got {body}"
        )
        assert body[0]["matching_snippet"] == _STORED_TEXT, (
            f"{label} must still serve the backed excerpt; got {body[0]}"
        )


# ---------------------------------------------------------------------------
# §A-DISC-10 — the rule behind §A-DISC-08/09, stated once for every ranked
# retrieval endpoint: a page of results is a budget, and it is spent only on
# results the caller may actually receive.
#
# Add your endpoint to the responses mapping in the test below when you add a
# ranked, user-facing retrieval surface. A surface that cuts its page before applying
# visibility and chunk backing gives those slots away for nothing: it answers
# short — or empty — while results the caller can see were available one rank
# further down.
#
# Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:153
#   (GET /api/similar enriches the whole candidate pool, then cuts the page)
# Verified: services/paper_ingestion/paper_ingestion/routers/discovery.py:254
#   (POST /api/discover enriches the whole candidate pool, then cuts the page)
# Verified: services/paper_ingestion/paper_ingestion/ingestion/search.py:687
#   (hybrid_search resolves every fused candidate, then cuts the page)
# ---------------------------------------------------------------------------

_RANKED_PAGE_SIZE = 3
_HIDDEN_LEADER_COUNT = 2
_UNMATCHED_QUERY = "zzzrankedretrievalprobe"


class _StubbedVectorLegEmbedder(EmbeddingSearchMixin):
    """Real hybrid fusion over a stubbed vector leg.

    Only the Qdrant call is replaced, so the endpoint still runs the fusion and
    the relational visibility recheck this test is about.
    """

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    async def search_chunks_global(self, *_args, **_kwargs) -> list[dict]:
        """Return the stubbed candidate chunks, ignoring the query and scope."""
        return list(self._chunks)


async def test_ranked_retrieval_endpoints_fill_the_page_after_filtering(
    contract_two_users, _pi_app, _configure_api_key, contract_conn
):
    """Every ranked endpoint returns a full page of visible results.

    The two highest-ranked candidates belong to another user and are not in the
    caller's library, so each endpoint must answer with the next three visible
    papers rather than surrendering the slots the hidden pair occupied.
    """
    hidden_ids = [
        await _seed_paper_with_chunks(
            contract_conn, contract_two_users.user_b_id, f"rank-h{i}", [0]
        )
        for i in range(_HIDDEN_LEADER_COUNT)
    ]
    visible_ids = [
        await _seed_library_paper(contract_conn, contract_two_users.user_a_id, f"rank-v{i}", [0])
        for i in range(_RANKED_PAGE_SIZE + 1)
    ]
    expected_page = visible_ids[:_RANKED_PAGE_SIZE]

    hits = [
        {
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": f"excerpt for {paper_id}",
            "score": 0.99 - index * 0.01,
        }
        for index, paper_id in enumerate(hidden_ids + visible_ids)
    ]

    async def _discover_from_seeds(*, limit, **_kwargs):
        # Mirrors discover_from_seeds: best score first, at most `limit` papers.
        return hits[:limit]

    _pi_app.state.embedder.search_similar = AsyncMock(return_value=hits)
    _pi_app.state.embedder.discover_from_seeds = AsyncMock(side_effect=_discover_from_seeds)
    _pi_app.state.embedder.hybrid_search = _StubbedVectorLegEmbedder(hits).hybrid_search

    seed_id = contract_two_users.paper_id_a
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        responses = {
            "GET /api/similar": await c.get(f"/api/similar/{seed_id}?limit={_RANKED_PAGE_SIZE}"),
            "POST /api/discover": await c.post(
                "/api/discover",
                json={"paper_ids": [seed_id], "limit": _RANKED_PAGE_SIZE},
            ),
            "POST /api/papers/search-hybrid": await c.post(
                "/api/papers/search-hybrid",
                json={"query": _UNMATCHED_QUERY, "max_results": _RANKED_PAGE_SIZE},
            ),
        }

    for label, resp in responses.items():
        assert resp.status_code == 200, (
            f"{label} expected 200; got {resp.status_code}: {resp.text[:300]}"
        )
        returned = [item.get("paper_id", item.get("id")) for item in resp.json()]
        assert returned == expected_page, (
            f"{label} must fill its page with the highest-ranked results the caller can "
            f"see; the slots taken by hidden candidates must not be lost. Got {returned}, "
            f"expected {expected_page}"
        )
