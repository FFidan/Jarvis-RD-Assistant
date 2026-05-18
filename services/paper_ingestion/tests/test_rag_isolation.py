"""User-scope isolation for the Qdrant-backed RAG layer.

Proves the search functions in ``paper_ingestion.ingestion.embedder`` honour
the ``user_id`` kwarg: caller user_id=A sees only A-tagged chunks + canonical
(NULL) chunks; never user-B chunks.

The BM25 leg of ``hybrid_search`` is intentionally OUT of this filter (Decision 6
of the 2026-05-14 RAG/topics/hygiene sweep — see
docs/plans/2026-05-14-functional-sweep-rag-topics-hygiene.md). User-level
paper visibility belongs at the router layer (e.g. papers/feed joins
paper_user_state), not at the embedder. The semantic leg is scoped; the
BM25 leg surfaces cross-corpus discovery for not-yet-claimed papers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from paper_ingestion.embedder import Embedder


def _hit(paper_id: int, user_id: int | None, score: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        payload={
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": f"chunk owned by {user_id}",
            "page_number": 1,
            "user_id": user_id,
        },
        score=score,
    )


def _make_embedder(hits: list[SimpleNamespace]) -> tuple[Embedder, AsyncMock]:
    """Return (embedder, mock_qdrant) so tests can inspect mock_qdrant.call_args."""
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points.return_value = SimpleNamespace(points=hits)
    embedder = Embedder(AsyncMock(), mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])
    return embedder, mock_qdrant


async def test_search_chunks_global_user_scope_passes_filter_to_qdrant():
    """search_chunks_global(user_id=1) sends a should=[user_id==1, is_null] filter."""
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    embedder, mock_qdrant = _make_embedder([])

    await embedder.search_chunks_global("q", user_id=1)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf is not None, "user_id should produce a non-None filter"
    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]
    assert len(user_clauses) == 1
    assert isinstance(user_clauses[0].match, MatchValue)
    assert user_clauses[0].match.value == 1
    assert len(null_clauses) == 1


async def test_search_chunks_global_no_user_id_no_filter():
    """Legacy single-tenant code path: no user_id → no filter."""
    embedder, mock_qdrant = _make_embedder([_hit(1, 2), _hit(2, 5), _hit(3, None)])

    results = await embedder.search_chunks_global("q")

    assert mock_qdrant.query_points.call_args.kwargs["query_filter"] is None
    assert len(results) == 3


async def test_search_similar_excludes_seed_paper_and_scopes_to_user():
    """must_not[paper_id] rides alongside should[user_id OR null]."""
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    embedder, mock_qdrant = _make_embedder([])

    await embedder.search_similar("q", paper_id_filter=42, user_id=7)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf.must_not[0].key == "paper_id"
    must_not_clause = qf.must_not[0]
    assert isinstance(must_not_clause, FieldCondition)
    assert isinstance(must_not_clause.match, MatchValue)
    assert must_not_clause.match.value == 42
    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]
    assert len(user_clauses) == 1
    assert isinstance(user_clauses[0].match, MatchValue)
    assert user_clauses[0].match.value == 7
    assert len(null_clauses) == 1


async def test_hybrid_search_threads_user_id_to_semantic_leg_only():
    """hybrid_search passes user_id to search_chunks_global; BM25 SQL is unchanged."""
    embedder, _mock_qdrant = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db_pool.acquire.return_value.__aenter__.return_value = conn

    await embedder.hybrid_search("neural odes", db_pool=db_pool, limit=5, user_id=7)

    assert embedder.search_chunks_global.call_args.kwargs["user_id"] == 7


async def test_hybrid_search_bm25_leg_does_not_filter_by_user_id():
    """BM25 leg's SQL string must not reference user_id (Decision 6, 2026-05-14 sweep).

    Per-user paper visibility is enforced at the router layer (papers/feed
    endpoint joins paper_user_state); the BM25 leg of hybrid_search
    intentionally surfaces cross-corpus matches so that searches against
    not-yet-claimed papers still work. Regression guard: if someone adds
    a WHERE user_id = $N to the BM25 SQL, this test fails.
    """
    embedder, _mock_qdrant = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    captured_sql: list[str] = []

    async def _capture(sql, *args, **kwargs):
        captured_sql.append(sql)
        return []

    conn.fetch = AsyncMock(side_effect=_capture)
    db_pool.acquire.return_value.__aenter__.return_value = conn

    await embedder.hybrid_search("neural odes", db_pool=db_pool, limit=5, user_id=42)

    assert captured_sql, "BM25 SQL should have been captured"
    sql_text = " ".join(captured_sql).lower()
    assert "user_id" not in sql_text, (
        "BM25 SQL must not reference user_id (Decision 6, 2026-05-14 sweep). "
        f"Captured SQL: {captured_sql}"
    )
    assert "$3" not in sql_text, "BM25 SQL should only have $1 and $2 parameters"


async def test_prepare_cross_paper_rag_threads_user_id():
    """prepare_cross_paper_rag forwards user_id into search_chunks_global."""
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import prepare_cross_paper_rag

    embedder, _mock_qdrant = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db_pool.acquire.return_value.__aenter__.return_value = conn

    body = CrossPaperAskRequest(
        question="how do neural ODEs handle stiffness?",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )
    http_client = AsyncMock()

    result = await prepare_cross_paper_rag(embedder, db_pool, body, http_client, user_id=99)

    assert embedder.search_chunks_global.call_args.kwargs["user_id"] == 99
    # No chunks returned → expect the no-results short-circuit
    assert getattr(result, "answer", "").startswith("No relevant")


async def test_cross_paper_rag_two_tenant_isolation():
    """Two-tenant isolation: tenant A's query must not see tenant B's private chunks.

    Regression guard for the Filter(should=[user_id==X, is_null]) scoping in
    ``search_chunks_global``.  If ``_user_scope_filter`` is bypassed or its
    ``should`` clauses are removed, Qdrant would return B's chunk to A, and
    this test fails.

    Mechanism: we let Qdrant return the raw chunk list (bypassing the real
    Qdrant client) and then verify that the Qdrant ``query_filter`` passed for
    tenant A's call would exclude tenant B's chunks — i.e. the filter has a
    ``FieldCondition(user_id == A)`` and an ``IsNullCondition``, but no
    unconditional match for B.
    """
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    tenant_a = 1
    tenant_b = 2

    # Canonical chunk (user_id=None) — should be visible to everyone
    chunk_canonical = _hit(paper_id=20, user_id=None, score=0.80)

    # The real Qdrant client applies the filter server-side; here we verify
    # the filter *passed* to query_points enforces the correct scoping for tenant_a.
    embedder, mock_qdrant = _make_embedder([chunk_canonical])  # Qdrant "returns" only canonical
    await embedder.search_chunks_global("attention mechanisms", user_id=tenant_a)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]

    # Must have a non-null filter (proves scoping is active for tenant_a)
    assert qf is not None, "No filter passed — cross-tenant leak possible"

    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]

    # Exactly one user-match clause, scoped to tenant_a — NOT tenant_b
    assert len(user_clauses) == 1, f"Expected 1 FieldCondition, got {user_clauses}"
    assert isinstance(user_clauses[0].match, MatchValue)
    assert user_clauses[0].match.value == tenant_a, (
        f"Filter scoped to wrong user: {user_clauses[0].match.value!r} != {tenant_a}"
    )
    assert user_clauses[0].match.value != tenant_b, "Filter would admit tenant B's chunks"

    # Canonical (null) clause present so shared corpus remains visible
    assert len(null_clauses) == 1, "Missing IsNullCondition — canonical chunks would be excluded"

    # No clause that would unconditionally admit tenant_b's private chunks
    b_clauses = [
        c
        for c in qf.should
        if isinstance(c, FieldCondition)
        and isinstance(c.match, MatchValue)
        and c.match.value == tenant_b
    ]
    assert len(b_clauses) == 0, (
        "Filter contains a clause that would admit tenant B's private chunks"
    )


# ---------------------------------------------------------------------------
# RAG-DB-1: metadata fetch respects user_id scope (defense-in-depth)
# ---------------------------------------------------------------------------


def _make_db_row(paper_id: int, title: str, user_id: int | None) -> dict:
    """Simulate an asyncpg Record as a plain dict for test purposes."""
    return {
        "id": paper_id,
        "title": title,
        "authors": "Author",
        "url": "http://x",
        "user_id": user_id,
    }


def _make_pool_with_chunks(
    chunks: list[dict],
    db_rows: list[dict],
) -> tuple:
    """Return (embedder, db_pool) mocks for prepare_cross_paper_rag tests."""
    mock_qdrant = AsyncMock()
    from paper_ingestion.embedder import Embedder

    embedder = Embedder(AsyncMock(), mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])
    embedder.search_chunks_global = AsyncMock(return_value=chunks)
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, c, top_k: c[:top_k])

    conn = AsyncMock()
    # Capture the SQL and params so we can assert on them
    captured: list[tuple] = []

    async def _fetch(sql, *args, **kwargs):
        captured.append((sql, args))
        return db_rows

    conn.fetch = AsyncMock(side_effect=_fetch)
    db_pool = MagicMock()
    db_pool.acquire.return_value.__aenter__.return_value = conn
    return embedder, db_pool, captured


async def test_metadata_fetch_excludes_other_user_paper():
    """RAG-DB-1 (a): metadata SQL uses user_library predicate; denied chunk is dropped.

    A paper not in the caller's user_library and not canonical (i.e. owned by
    someone else) is invisible: the mock DB returns no rows (simulating the
    user_library EXISTS predicate filtering it out). We verify:
      - the SQL uses the user_library join table (not a phantom papers.user_id column)
      - the owned arm correlates ul.user_id = $2
      - the canonical arm is a NOT EXISTS on user_library
      - the $2 positional parameter carries the caller's user_id
      - the denied chunk does NOT appear in sources (defense-in-depth filter)
    """
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagNoResults, prepare_cross_paper_rag

    # Qdrant returns a chunk for paper_id=5 (mis-tagged — owned by a different user)
    chunks = [
        {
            "paper_id": 5,
            "chunk_index": 0,
            "content": "content from other-user paper 5",
            "page_number": 1,
            "score": 0.9,
        }
    ]
    # DB returns empty: the user_library predicate denies paper 5 for user 99
    db_rows: list[dict] = []

    embedder, db_pool, captured = _make_pool_with_chunks(chunks, db_rows)

    body = CrossPaperAskRequest(
        question="test question",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )

    result = await prepare_cross_paper_rag(embedder, db_pool, body, AsyncMock(), user_id=99)

    assert captured, "DB fetch was not called"
    sql, args = captured[0]
    sql_lower = sql.lower()

    # Current schema: ownership via user_library join table (not papers.user_id column)
    assert "user_library" in sql_lower, (
        f"Metadata SQL must query user_library (not papers.user_id): {sql!r}"
    )
    # Owned-paper arm: EXISTS (... ul.user_id = $2)
    assert "ul.user_id = $2" in sql_lower, (
        f"Missing ul.user_id = $2 in owned-paper EXISTS arm: {sql!r}"
    )
    # Canonical-paper arm: NOT EXISTS on user_library (paper in nobody's library)
    assert "not exists" in sql_lower, f"Missing NOT EXISTS canonical arm in metadata SQL: {sql!r}"
    # $2 positional arg is the caller's user_id
    assert args[1] == 99, f"Expected user_id=99 as $2 parameter, got args={args}"

    # Defense-in-depth: denied chunk must not surface in sources
    assert isinstance(result, CrossPaperRagNoResults), (
        f"Expected no-results short-circuit when all chunks are DB-denied, got {result!r}"
    )
    assert result.sources == [], "Denied paper must not appear in sources"


async def test_metadata_fetch_includes_canonical_paper():
    """RAG-DB-1 (b): canonical papers (in nobody's library) are included for all users.

    A paper that exists in the papers table but appears in no user_library row is
    canonical — the NOT EXISTS arm of the predicate allows it through for every
    caller.  We verify:
      - the SQL uses user_library (not a phantom papers.user_id column)
      - the NOT EXISTS canonical arm is present
      - the owned arm correlates ul.user_id = $2
      - $2 carries the caller's user_id
      - the canonical paper's title surfaces in sources (shared corpus accessible)
    """
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    chunks = [
        {
            "paper_id": 20,
            "chunk_index": 0,
            "content": "canonical content",
            "page_number": 3,
            "score": 0.85,
        }
    ]
    # DB returns the canonical paper (NOT EXISTS arm allows it through)
    db_rows = [{"id": 20, "title": "Canonical Paper", "authors": "System", "url": "http://c"}]

    embedder, db_pool, captured = _make_pool_with_chunks(chunks, db_rows)

    body = CrossPaperAskRequest(
        question="canonical question",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )

    result = await prepare_cross_paper_rag(embedder, db_pool, body, AsyncMock(), user_id=7)

    assert isinstance(result, CrossPaperRagPrep), f"Expected CrossPaperRagPrep, got {result!r}"
    titles = [s["paper_title"] for s in result.sources]
    assert "Canonical Paper" in titles, (
        f"Canonical paper missing from sources — shared corpus broken: {titles}"
    )

    # SQL shape: user_library join table, owned arm, NOT EXISTS canonical arm
    sql, args = captured[0]
    sql_lower = sql.lower()
    assert "user_library" in sql_lower, (
        f"Metadata SQL must query user_library (not papers.user_id): {sql!r}"
    )
    assert "ul.user_id = $2" in sql_lower, (
        f"Missing ul.user_id = $2 owned-paper EXISTS arm: {sql!r}"
    )
    assert "not exists" in sql_lower, f"Missing NOT EXISTS canonical arm in metadata SQL: {sql!r}"
    assert args[1] == 7, f"Expected user_id=7 as $2 parameter, got args={args}"


async def test_metadata_fetch_same_user_paper_unaffected():
    """RAG-DB-1 (c): regression — same-user papers are still returned normally."""
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    chunks = [
        {
            "paper_id": 42,
            "chunk_index": 0,
            "content": "user's own paper content",
            "page_number": 2,
            "score": 0.92,
        }
    ]
    db_rows = [{"id": 42, "title": "My Paper", "authors": "Me", "url": "http://me"}]

    embedder, db_pool, captured = _make_pool_with_chunks(chunks, db_rows)

    body = CrossPaperAskRequest(
        question="my own research",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )

    result = await prepare_cross_paper_rag(embedder, db_pool, body, AsyncMock(), user_id=5)

    assert isinstance(result, CrossPaperRagPrep), f"Expected CrossPaperRagPrep, got {result!r}"
    titles = [s["paper_title"] for s in result.sources]
    assert "My Paper" in titles, f"Same-user paper missing from sources: {titles}"
    sql, args = captured[0]
    assert args[1] == 5, f"Expected user_id=5 as $2, got {args}"


# ---------------------------------------------------------------------------
# RAG-DB-1 defense-in-depth: stale/mis-tagged Qdrant payloads must not leak
# ---------------------------------------------------------------------------


async def test_cross_paper_rag_drops_db_denied_chunk():
    """Defense-in-depth: chunks whose paper_id is absent from DB auth result are dropped.

    Scenario: Qdrant returns two chunks — one for an authorized paper (paper_id=10,
    present in the mocked DB rows) and one for a DB-denied paper (paper_id=99, absent
    from DB rows, simulating a stale/mis-tagged Qdrant payload for a paper the caller
    does not own and is not canonical).

    Expected outcomes:
      1. The denied chunk's content does NOT appear in the LLM prompt messages.
      2. The denied paper's id does NOT appear in sources.
      3. When ALL selected chunks are denied (only paper_id=99 present), the
         function returns CrossPaperRagNoResults with empty sources.

    ``search_chunks_global`` is mocked (via _make_pool_with_chunks) so
    ``selected_chunks`` is fully controlled without touching Qdrant or BM25.
    """
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import (
        CrossPaperRagNoResults,
        CrossPaperRagPrep,
        prepare_cross_paper_rag,
    )

    # --- Part 1: mixed — one authorized chunk + one denied chunk ---
    authorized_chunk = {
        "paper_id": 10,
        "chunk_index": 0,
        "content": "authorized content from paper 10",
        "page_number": 1,
        "score": 0.95,
    }
    denied_chunk = {
        "paper_id": 99,
        "chunk_index": 0,
        "content": "leaked content from denied paper 99",
        "page_number": 1,
        "score": 0.88,
    }

    # DB returns metadata only for paper_id=10; paper_id=99 is absent (denied)
    db_rows_partial = [{"id": 10, "title": "Authorized Paper", "authors": "A", "url": "http://a"}]

    embedder, db_pool, _captured = _make_pool_with_chunks(
        [authorized_chunk, denied_chunk], db_rows_partial
    )

    body = CrossPaperAskRequest(
        question="test leak",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )

    result = await prepare_cross_paper_rag(embedder, db_pool, body, AsyncMock(), user_id=3)

    assert isinstance(result, CrossPaperRagPrep), (
        f"Expected CrossPaperRagPrep (one authorized chunk passes), got {result!r}"
    )

    # Denied chunk content must not appear in the LLM prompt
    prompt_text = " ".join(m["content"] for m in result.messages)
    assert "leaked content from denied paper 99" not in prompt_text, (
        "Denied paper content leaked into LLM prompt"
    )

    # Denied paper must not appear in sources
    source_paper_ids = [s["paper_id"] for s in result.sources]
    assert 99 not in source_paper_ids, f"Denied paper_id=99 appeared in sources: {result.sources}"

    # Authorized paper is still present
    assert 10 in source_paper_ids, f"Authorized paper_id=10 missing from sources: {result.sources}"

    # --- Part 2: all-denied — every selected chunk belongs to a denied paper ---
    embedder_all_denied, db_pool_all_denied, _cap2 = _make_pool_with_chunks(
        [denied_chunk],
        [],  # DB returns nothing — paper_id=99 denied entirely
    )

    result_all_denied = await prepare_cross_paper_rag(
        embedder_all_denied, db_pool_all_denied, body, AsyncMock(), user_id=3
    )

    assert isinstance(result_all_denied, CrossPaperRagNoResults), (
        f"Expected CrossPaperRagNoResults when all chunks denied, got {result_all_denied!r}"
    )
    assert result_all_denied.sources == [], (
        "No-results object must have empty sources when all chunks are DB-denied"
    )
