"""Embedder boundary contracts backed by faux LiteLLM and faux Qdrant sidecars.

Survivor-of: mock-heavy embedder/Qdrant tests that asserted calls against
``AsyncMock`` clients instead of exercising the real Embedder boundary flow.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_VISIBILITY_GENERATION = "a" * 32


async def _current_visibility_generation() -> str:
    """Return the fixed generation shared by isolated vector tests."""
    return _VISIBILITY_GENERATION


def _private_visibility(source_type: str):
    """Build complete private-vector metadata for a test paper."""
    from paper_ingestion.ingestion.payload_schema import VectorVisibility

    return VectorVisibility(source_type, "private", _VISIBILITY_GENERATION)


async def test_embedder_sidecars_store_and_search_user_scoped_vectors(monkeypatch):
    """Embedder uses real HTTP embeddings plus Qdrant-compatible search/storage.

    # Verified: services/paper_ingestion/paper_ingestion/ingestion/embed_store.py:107
    # Verified: services/paper_ingestion/paper_ingestion/ingestion/search.py:130
    # Verified: services/paper_ingestion/paper_ingestion/ingestion/search.py:288
    """
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant = FauxQdrantClient()
            embedder = Embedder(
                http_client,
                qdrant,
                visibility_generation_provider=_current_visibility_generation,
            )
            await embedder.ensure_collection()

            await embedder.embed_and_store(
                10,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="alpha methods and reproducibility",
                        page_number=1,
                        start_char=0,
                        end_char=33,
                    ),
                    ChunkForEmbedding(
                        chunk_index=1,
                        content="beta results and limitations",
                        page_number=2,
                        start_char=34,
                        end_char=62,
                    ),
                ],
                user_id=7,
                visibility=_private_visibility("arxiv"),
            )
            await embedder.embed_and_store(
                11,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="private paper for a different user",
                        page_number=1,
                        start_char=0,
                        end_char=34,
                    )
                ],
                user_id=8,
                visibility=_private_visibility("upload"),
            )

            in_paper = await embedder.search_chunks_in_paper(
                "alpha reproducibility",
                paper_id=10,
                limit=5,
                score_threshold=0.0,
            )
            scoped_global = await embedder.search_chunks_global(
                "paper",
                user_id=7,
                library_paper_ids=[10],
                limit=10,
                score_threshold=0.0,
            )

    assert {row["chunk_index"] for row in in_paper} == {0, 1}
    assert {row["paper_id"] for row in scoped_global} == {10}


# ---------------------------------------------------------------------------
# PI-RAG-001 — cross-paper RAG scope widen + leak guard (end-to-end)
#
# Proves through the REAL prepare_cross_paper_rag path (real Embedder + faux
# Qdrant + real DB visibility check) that:
#   POSITIVE: caller B retrieves private paper P that user A embedded because
#             P is also present in B's user_library.
#   NEGATIVE: a paper Q PRIVATE to A (in A's library only, NOT B's) is NOT
#             retrievable by B — neither via the widened Qdrant branch (Q not
#             in B's library) nor past the defense-in-depth DB visibility check.
#
# Verified: services/paper_ingestion/paper_ingestion/ingestion/embedding_config.py:117
#   (_user_scope_filter adds paper_id MatchAny branch from caller's library)
# Verified: services/paper_ingestion/paper_ingestion/rag/streaming.py:199
#   (prepare_cross_paper_rag queries user_library for caller, threads list down)
# Verified: services/paper_ingestion/paper_ingestion/rag/streaming.py:287
#   (defense-in-depth DB visibility predicate — the backstop)
# ---------------------------------------------------------------------------


async def test_cross_paper_rag_widens_to_callers_library_but_not_others_private(
    contract_conn, monkeypatch
):
    """Caller B retrieves a joint-library paper but never A-only private Q."""
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import ChunkForEmbedding, CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    user_a = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('rag-a@test', 'user') RETURNING id"
    )
    user_b = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('rag-b@test', 'user') RETURNING id"
    )

    # Paper P is in both libraries; paper Q is an A-only private upload.
    paper_p = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rag-shared-P', 'arxiv', 'Shared Reproducibility Paper', '{}',
                   'https://rag.test/p') RETURNING id"""
    )
    paper_q = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rag-private-Q', 'upload', 'A Private Reproducibility Upload', '{}',
                   'https://rag.test/q') RETURNING id"""
    )

    # Library membership: A has BOTH P and Q; B has ONLY P.
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES "
        "($1, $2, 'manual_save'), ($1, $3, 'manual_save'), ($4, $2, 'manual_save')",
        user_a,
        paper_p,
        paper_q,
        user_b,
    )

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant = FauxQdrantClient()
            embedder = Embedder(
                http_client,
                qdrant,
                visibility_generation_provider=_current_visibility_generation,
            )
            await embedder.ensure_collection()

            # Both vectors retain A as legacy attribution, but authorization
            # depends only on complete private-scope metadata plus membership.
            await embedder.embed_and_store(
                paper_p,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="reproducibility methods shared across the corpus",
                        page_number=1,
                        start_char=0,
                        end_char=48,
                    )
                ],
                user_id=user_a,
                visibility=_private_visibility("arxiv"),
            )
            await embedder.embed_and_store(
                paper_q,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="reproducibility notes private to user A only",
                        page_number=1,
                        start_char=0,
                        end_char=45,
                    )
                ],
                user_id=user_a,
                visibility=_private_visibility("upload"),
            )

            # Isolate filter behaviour: identity rerank (no reranker config / network).
            embedder.rerank_chunks = _identity_rerank  # type: ignore[method-assign]

            pool = SharedConnPool(contract_conn)
            body = CrossPaperAskRequest(
                question="reproducibility",
                max_chunks=10,
                max_papers=5,
                decompose=False,
            )
            result = await prepare_cross_paper_rag(
                embedder, pool, body, http_client, user_id=user_b
            )

    assert isinstance(result, CrossPaperRagPrep), f"expected results, got {result!r}"
    retrieved_paper_ids = {s["paper_id"] for s in result.sources}

    # POSITIVE — B retrieves paper P from B's library although A embedded it.
    assert paper_p in retrieved_paper_ids, (
        "caller B must retrieve paper P from B's library despite A embedding it — "
        "this is the PI-RAG-001 under-fetch the widening fixes"
    )
    # NEGATIVE / no-leak — Q is private to A; B must never see it.
    assert paper_q not in retrieved_paper_ids, (
        "caller B must NOT retrieve paper Q (private to A, not in B's library) — "
        "leak guard: widening is keyed on the caller's OWN library only"
    )


async def _identity_rerank(query, chunks, top_k):  # noqa: ARG001
    """Async identity rerank stub: preserve input order, truncate to top_k."""
    return chunks[:top_k]


# ---------------------------------------------------------------------------
# search_chunks_in_paper defense-in-depth user scope
#
# Same joint-library topology as the PI-RAG-001 cross-paper test above, but at
# the paper-scoped search boundary:
#   POSITIVE: caller B retrieves chunks of library paper P that user A embedded
#             because P is in B's library — guards the
#             RAG-DB-1 regression class (a scoping "fix" must never break
#             legitimate library retrieval).
#   EXCLUSION: paper Q (embedded by A, NOT in B's library) yields NOTHING for
#             B when scope params are passed.
#   DEFAULT:  no user params → identical unscoped behaviour (extraction/core.py
#             system-context path stays default-None).
#
# Verified: services/paper_ingestion/paper_ingestion/ingestion/search.py:117
#   (search_chunks_in_paper nests _user_scope_filter as a `must` sub-Filter)
# Verified: services/paper_ingestion/paper_ingestion/extraction/core.py:167
#   (extraction calls search_chunks_in_paper without user scope — default-None)
# ---------------------------------------------------------------------------


async def test_search_chunks_in_paper_user_scope_shared_corpus_exclusion_and_default(
    monkeypatch,
):
    """Paper search permits caller-library vectors and excludes other private vectors."""
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    user_a, user_b = 7, 8
    paper_p, paper_q = 10, 11  # P: in B's library; Q: private to A

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant = FauxQdrantClient()
            embedder = Embedder(
                http_client,
                qdrant,
                visibility_generation_provider=_current_visibility_generation,
            )
            await embedder.ensure_collection()

            # Both vectors retain A as legacy attribution; neither is authorized by it.
            await embedder.embed_and_store(
                paper_p,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="reproducibility methods shared across the corpus",
                        page_number=1,
                        start_char=0,
                        end_char=48,
                    )
                ],
                user_id=user_a,
                visibility=_private_visibility("arxiv"),
            )
            await embedder.embed_and_store(
                paper_q,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="reproducibility notes private to user A only",
                        page_number=1,
                        start_char=0,
                        end_char=45,
                    )
                ],
                user_id=user_a,
                visibility=_private_visibility("upload"),
            )

            # POSITIVE — B's library widening retrieves A-embedded paper P.
            shared = await embedder.search_chunks_in_paper(
                "reproducibility",
                paper_id=paper_p,
                limit=5,
                score_threshold=0.0,
                user_id=user_b,
                library_paper_ids=[paper_p],
            )
            # EXCLUSION — Q not in B's library, chunks owned by A: nothing for B.
            excluded = await embedder.search_chunks_in_paper(
                "reproducibility",
                paper_id=paper_q,
                limit=5,
                score_threshold=0.0,
                user_id=user_b,
                library_paper_ids=[paper_p],
            )
            # DEFAULT-None — positional call shape of extraction/core.py:167.
            unscoped = await embedder.search_chunks_in_paper(
                "reproducibility", paper_q, limit=3, score_threshold=0.0
            )

    assert {row["chunk_index"] for row in shared} == {0}, (
        "caller B must retrieve library paper P's chunks despite A embedding them — "
        "RAG-DB-1 class guard: user scoping must not break legitimate retrieval"
    )
    assert excluded == [], (
        "caller B must get NOTHING from paper Q (embedded by A, not in B's library) — "
        "M7 defense-in-depth even when route-level ownership checks were bypassed"
    )
    assert {row["chunk_index"] for row in unscoped} == {0}, (
        "default-None (system-context extraction path) must behave exactly as before"
    )


# ---------------------------------------------------------------------------
# prepare_single_paper_rag end-to-end: caller fetches library + threads scope
#
# Verified: services/paper_ingestion/paper_ingestion/rag/streaming.py:124-137
#   (prepare_single_paper_rag queries user_library for the caller, threads
#    user_id + library_paper_ids into search_chunks_in_paper)
# ---------------------------------------------------------------------------


async def test_single_paper_rag_scoped_to_callers_library_not_others_private(
    contract_conn, monkeypatch
):
    """B can ask about joint-library P but receives no chunks from A-only Q."""
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import AskRequest, ChunkForEmbedding
    from paper_ingestion.rag.exceptions import NoRelevantChunksError
    from paper_ingestion.rag.streaming import prepare_single_paper_rag

    user_a = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('rag-single-a@test', 'user') RETURNING id"
    )
    user_b = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('rag-single-b@test', 'user') RETURNING id"
    )
    paper_p = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rag-single-shared-P', 'arxiv', 'Shared Single-Paper RAG Paper', '{}',
                   'https://rag.test/sp') RETURNING id"""
    )
    paper_q = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rag-single-private-Q', 'upload', 'A Private Single-Paper Upload', '{}',
                   'https://rag.test/sq') RETURNING id"""
    )
    # Library membership: A has BOTH P and Q; B has ONLY P.
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES "
        "($1, $2, 'manual_save'), ($1, $3, 'manual_save'), ($4, $2, 'manual_save')",
        user_a,
        paper_p,
        paper_q,
        user_b,
    )

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant = FauxQdrantClient()
            embedder = Embedder(
                http_client,
                qdrant,
                visibility_generation_provider=_current_visibility_generation,
            )
            await embedder.ensure_collection()

            # Both vectors retain A as legacy attribution; neither is authorized by it.
            await embedder.embed_and_store(
                paper_p,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="reproducibility methods shared across the corpus",
                        page_number=1,
                        start_char=0,
                        end_char=48,
                    )
                ],
                user_id=user_a,
                visibility=_private_visibility("arxiv"),
            )
            await embedder.embed_and_store(
                paper_q,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="reproducibility notes private to user A only",
                        page_number=1,
                        start_char=0,
                        end_char=45,
                    )
                ],
                user_id=user_a,
                visibility=_private_visibility("upload"),
            )

            embedder.rerank_chunks = _identity_rerank  # type: ignore[method-assign]
            pool = SharedConnPool(contract_conn)
            body = AskRequest(question="reproducibility", max_chunks=5)

            # POSITIVE — B retrieves joint-library P although A embedded its chunks.
            messages, sources = await prepare_single_paper_rag(
                embedder,
                pool,
                paper_id=paper_p,
                body=body,
                http_client=http_client,
                user_id=user_b,
            )
            # EXCLUSION — Q is private to A; defense-in-depth fails closed for B
            # even though this call bypassed the route-level ownership check.
            with pytest.raises(NoRelevantChunksError):
                await prepare_single_paper_rag(
                    embedder,
                    pool,
                    paper_id=paper_q,
                    body=body,
                    http_client=http_client,
                    user_id=user_b,
                )

    assert len(sources) >= 1, (
        "caller B must get sources for paper P in B's library — "
        "RAG-DB-1 class guard: scope threading must not break legitimate asks"
    )
    assert all("private to user A" not in s["content"] for s in sources), (
        "no chunk content from A-private paper Q may surface in B's sources"
    )
    assert len(messages) == 2


# ---------------------------------------------------------------------------
# §W1A.4-REEMBED-01 — reembed_paper Qdrant failure rolls back DB
#
# Verified: scripts/reembed.py:622-667 (reembed_paper: Qdrant upsert first,
#   DB update last; stale-point delete failure raises ScriptError before step 5)
# Survivor-of: ~3-4 mock-units in test_reembed.py that patch DB/Qdrant with
#   MagicMock and check call order
# ---------------------------------------------------------------------------


async def test_reembed_swap_atomicity_qdrant_failure_rolls_back_db(contract_conn, monkeypatch):
    """reembed_paper must NOT update DB embedding_id when Qdrant delete fails.

    The atomicity invariant (scripts/reembed.py:622-667):
      step 3 upsert new Qdrant points → step 4 delete old Qdrant points →
      step 5 update DB embedding_id + embedding_model (in a transaction).
    If step 4 raises ScriptError (REEMBED_CONTINUE_ON_ERROR=false),
    the exception propagates before step 5 executes.
    DB row retains original embedding_id, so a retry can re-process the same
    stale IDs deterministically.

    SharedConnPool wraps the per-test txn connection so all DB reads and writes
    are within the rollback boundary — no persistent state leak.

    # Verified: scripts/reembed.py:631-645 (stale-point delete + ScriptError raise)
    # Verified: scripts/reembed.py:648-668 (DB UPDATE in transaction — step 5)
    """
    import sys
    from pathlib import Path

    import httpx
    import pytest as _pytest

    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    # Seed paper + chunk with a known old embedding_id (within the test txn)
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('reembed-atom-01', 'arxiv', 'Atomicity Paper', '{}',
                   'https://reembed.test/1')
           RETURNING id"""
    )
    old_embed_id = "aaaaaaaa-0000-0000-0000-000000000001"
    chunk_db_id = await contract_conn.fetchval(
        """INSERT INTO paper_chunks
           (paper_id, chunk_index, content, page_number, start_char, end_char,
            embedding_id, embedding_model)
           VALUES ($1, 0, 'atomicity test content', 1, 0, 22, $2, 'old-model')
           RETURNING id""",
        paper_id,
        old_embed_id,
    )

    # Faux Qdrant whose delete() always raises — simulates a Qdrant failure
    # mid-swap (new points already upserted, old-point cleanup fails)
    faux_qdrant = FauxQdrantClient()

    async def _failing_delete(**_kwargs):
        raise RuntimeError("Qdrant delete unavailable")

    faux_qdrant.delete = _failing_delete  # type: ignore[method-assign]

    # Add repo root to sys.path so `import scripts.reembed` resolves.
    # File is services/paper_ingestion/tests/contract/test_*.py → parents[4] is repo root.
    _repo_root = str(Path(__file__).resolve().parents[4])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        monkeypatch.setenv("REEMBED_CONTINUE_ON_ERROR", "false")

        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)
        from scripts.reembed import ScriptError, reembed_paper  # noqa: PLC0415

        # Ensure the faux Qdrant collection exists with the correct dimension
        await faux_qdrant.create_collection(
            collection_name="paper_chunks",
            vectors_config=type("_VP", (), {"size": EMBEDDING_DIMENSION, "distance": "Cosine"})(),
        )

        shared_pool = SharedConnPool(contract_conn)
        async with httpx.AsyncClient() as http_client:
            # Act: ScriptError must be raised because Qdrant delete fails (step 4)
            with _pytest.raises(ScriptError, match="Failed to delete old Qdrant"):
                await reembed_paper(
                    paper_id,
                    shared_pool,
                    faux_qdrant,
                    http_client,
                    visibility_generation=_VISIBILITY_GENERATION,
                )

    # Assert: DB embedding_id must still be the old value — step 5 was never reached
    row = await contract_conn.fetchrow(
        "SELECT embedding_id, embedding_model FROM paper_chunks WHERE id = $1",
        chunk_db_id,
    )
    assert row["embedding_id"] == old_embed_id, (
        f"DB embedding_id must remain '{old_embed_id}' when Qdrant delete raises; "
        f"got {row['embedding_id']!r}. This means step 5 executed despite step 4 failing — "
        "the atomicity invariant (scripts/reembed.py:631-668) is broken."
    )
    assert row["embedding_model"] == "old-model", (
        f"DB embedding_model must remain 'old-model'; got {row['embedding_model']!r}"
    )


# ---------------------------------------------------------------------------
# happy-path: FauxOllama + FauxQdrant end-to-end
#
# Verified: scripts/reembed.py:594-668 (reembed_paper: embed → upsert → delete → DB)
# Survivor-of: test_old_qdrant_points_deleted, test_db_embedding_model_updated,
#   test_reembed_qdrant_writes_wait_for_completion_before_db_update (mock-units)
# ---------------------------------------------------------------------------


# Verified: scripts/reembed.py:576-668
async def test_reembed_w2_happy_path_with_faux_ollama_and_faux_qdrant(contract_conn, monkeypatch):
    """reembed_paper embeds via FauxOllama, stores in FauxQdrant, updates DB embedding_model."""
    import sys
    from pathlib import Path

    import httpx

    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    _repo_root = str(Path(__file__).resolve().parents[4])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('reembed-w2-hp-01', 'arxiv', 'Happy Path Paper', '{}',
                   'https://reembed.test/hp1')
           RETURNING id"""
    )
    chunk_id = await contract_conn.fetchval(
        """INSERT INTO paper_chunks
           (paper_id, chunk_index, content, page_number, start_char, end_char,
            embedding_id, embedding_model)
           VALUES ($1, 0, 'happy path content', 1, 0, 18, 'old-uuid-hp', 'old-model')
           RETURNING id""",
        paper_id,
    )

    faux_qdrant = FauxQdrantClient()
    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)

        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)
        from scripts.reembed import reembed_paper  # noqa: PLC0415

        await faux_qdrant.create_collection(
            collection_name="paper_chunks",
            vectors_config=type("_VP", (), {"size": EMBEDDING_DIMENSION, "distance": "Cosine"})(),
        )
        shared_pool = SharedConnPool(contract_conn)
        async with httpx.AsyncClient() as http_client:
            count = await reembed_paper(
                paper_id,
                shared_pool,
                faux_qdrant,
                http_client,
                visibility_generation=_VISIBILITY_GENERATION,
            )

    assert count == 1
    row = await contract_conn.fetchrow(
        "SELECT embedding_id, embedding_model FROM paper_chunks WHERE id = $1", chunk_id
    )
    assert row["embedding_model"] == reembed_mod.EMBEDDING_MODEL_NAME
    assert row["embedding_id"] != "old-uuid-hp", "embedding_id must be updated to new point id"
    # New point must exist in FauxQdrant
    coll_count = await faux_qdrant.count(collection_name="paper_chunks")
    assert coll_count.count == 1


# ---------------------------------------------------------------------------
# upsert failure rolls back DB
#
# Verified: scripts/reembed.py:614-619 (upsert loop raises before delete/DB step)
# Survivor-of: test_reembed_partial_failure_preserves_old_points,
#   test_reembed_old_qdrant_delete_failure_is_fatal_by_default (mock-units)
# ---------------------------------------------------------------------------


# Verified: scripts/reembed.py:614-619
async def test_reembed_w2_rollback_semantics_on_qdrant_upsert_failure(contract_conn, monkeypatch):
    """reembed_paper must NOT update DB when Qdrant upsert raises mid-batch."""
    import sys
    from pathlib import Path

    import httpx
    import pytest as _pytest

    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    _repo_root = str(Path(__file__).resolve().parents[4])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('reembed-w2-upsert-fail', 'arxiv', 'Upsert Fail Paper', '{}',
                   'https://reembed.test/uf1')
           RETURNING id"""
    )
    old_embed_id = "bbbbbbbb-0000-0000-0000-000000000002"
    chunk_id = await contract_conn.fetchval(
        """INSERT INTO paper_chunks
           (paper_id, chunk_index, content, page_number, start_char, end_char,
            embedding_id, embedding_model)
           VALUES ($1, 0, 'upsert fail content', 1, 0, 19, $2, 'old-model')
           RETURNING id""",
        paper_id,
        old_embed_id,
    )

    faux_qdrant = FauxQdrantClient()

    async def _failing_upsert(**_kwargs):
        raise RuntimeError("Qdrant upsert unavailable")

    faux_qdrant.upsert = _failing_upsert  # type: ignore[method-assign]

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)

        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)
        from scripts.reembed import reembed_paper  # noqa: PLC0415

        await faux_qdrant.create_collection(
            collection_name="paper_chunks",
            vectors_config=type("_VP", (), {"size": EMBEDDING_DIMENSION, "distance": "Cosine"})(),
        )
        shared_pool = SharedConnPool(contract_conn)
        async with httpx.AsyncClient() as http_client:
            with _pytest.raises(RuntimeError, match="Qdrant upsert unavailable"):
                await reembed_paper(
                    paper_id,
                    shared_pool,
                    faux_qdrant,
                    http_client,
                    visibility_generation=_VISIBILITY_GENERATION,
                )

    row = await contract_conn.fetchrow(
        "SELECT embedding_id, embedding_model FROM paper_chunks WHERE id = $1", chunk_id
    )
    assert row["embedding_id"] == old_embed_id, (
        "DB embedding_id must stay unchanged when upsert fails before step 5"
    )
    assert row["embedding_model"] == "old-model"


# ---------------------------------------------------------------------------
# post-success Qdrant count == DB chunks count
#
# Verified: scripts/reembed.py:671-720 (verify_postconditions: DB count == Qdrant count)
# Survivor-of: test_verify_postconditions_requires_db_target_count_and_qdrant_count_parity,
#   test_main_runs_postcondition_after_successful_reembed (mock-units)
# ---------------------------------------------------------------------------


# Verified: scripts/reembed.py:671-720
async def test_reembed_w2_qdrant_post_state_matches_db_invariant(contract_conn, monkeypatch):
    """After reembed success, FauxQdrant collection size equals paper_chunks count for model."""
    import sys
    from pathlib import Path

    import httpx
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    _repo_root = str(Path(__file__).resolve().parents[4])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('reembed-w2-inv-01', 'arxiv', 'Invariant Paper', '{}',
                   'https://reembed.test/inv1')
           RETURNING id"""
    )
    for i in range(3):
        await contract_conn.execute(
            """INSERT INTO paper_chunks
               (paper_id, chunk_index, content, page_number, start_char, end_char,
                embedding_id, embedding_model)
               VALUES ($1, $2, $3, 1, 0, 10, $4, 'old-model')""",
            paper_id,
            i,
            f"chunk content {i}",
            f"old-inv-{i}",
        )

    faux_qdrant = FauxQdrantClient()
    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)

        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)
        from scripts.reembed import reembed_paper  # noqa: PLC0415

        await faux_qdrant.create_collection(
            collection_name="paper_chunks",
            vectors_config=type("_VP", (), {"size": EMBEDDING_DIMENSION, "distance": "Cosine"})(),
        )
        shared_pool = SharedConnPool(contract_conn)
        async with httpx.AsyncClient() as http_client:
            count = await reembed_paper(
                paper_id,
                shared_pool,
                faux_qdrant,
                http_client,
                visibility_generation=_VISIBILITY_GENERATION,
            )

    assert count == 3
    db_count = await contract_conn.fetchval(
        "SELECT count(*) FROM paper_chunks WHERE paper_id = $1 AND embedding_model = $2",
        paper_id,
        reembed_mod.EMBEDDING_MODEL_NAME,
    )
    qdrant_count = await faux_qdrant.count(
        collection_name="paper_chunks",
        count_filter=Filter(
            must=[
                FieldCondition(
                    key="embedding_model", match=MatchValue(value=reembed_mod.EMBEDDING_MODEL_NAME)
                )
            ]
        ),
    )
    assert int(db_count) == qdrant_count.count == 3, (
        f"DB chunks ({db_count}) must equal Qdrant vectors ({qdrant_count.count}) after reembed"
    )


# ---------------------------------------------------------------------------
# chunk shape preserved through pipeline
#
# Verified: scripts/reembed.py:594-612 (PointStruct payload keys + vector dim)
# Survivor-of: test_old_qdrant_points_deleted (payload key assertions),
#   test_embedder_payload_includes_model_name (mock-unit)
# ---------------------------------------------------------------------------


# Verified: scripts/reembed.py:594-612
async def test_reembed_w2_chunk_shape_preserved_through_pipeline(contract_conn, monkeypatch):
    """After reembed, Qdrant point has correct vector dim, payload keys, and chunk text."""
    import sys
    from pathlib import Path

    import httpx

    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    _repo_root = str(Path(__file__).resolve().parents[4])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('reembed-w2-shape-01', 'arxiv', 'Shape Paper', '{}',
                   'https://reembed.test/shape1')
           RETURNING id"""
    )
    content = "the exact chunk text must survive"
    await contract_conn.execute(
        """INSERT INTO paper_chunks
           (paper_id, chunk_index, content, page_number, start_char, end_char,
            embedding_id, embedding_model)
           VALUES ($1, 0, $2, 2, 4, 36, 'old-shape-id', 'old-model')""",
        paper_id,
        content,
    )

    faux_qdrant = FauxQdrantClient()
    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)

        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)
        from scripts.reembed import reembed_paper  # noqa: PLC0415

        await faux_qdrant.create_collection(
            collection_name="paper_chunks",
            vectors_config=type("_VP", (), {"size": EMBEDDING_DIMENSION, "distance": "Cosine"})(),
        )
        shared_pool = SharedConnPool(contract_conn)
        async with httpx.AsyncClient() as http_client:
            await reembed_paper(
                paper_id,
                shared_pool,
                faux_qdrant,
                http_client,
                visibility_generation=_VISIBILITY_GENERATION,
            )

    points, _ = await faux_qdrant.scroll(collection_name="paper_chunks", with_vectors=True)
    assert len(points) == 1
    pt = points[0]
    assert len(pt.vector) == EMBEDDING_DIMENSION, (
        f"vector dim must be {EMBEDDING_DIMENSION}, got {len(pt.vector)}"
    )
    required_keys = {
        "paper_id",
        "chunk_index",
        "content",
        "embedding_model",
        "page_number",
        "source_type",
        "visibility_scope",
        "visibility_generation",
        "embedding_fingerprint",
    }
    assert required_keys <= pt.payload.keys(), f"missing keys: {required_keys - pt.payload.keys()}"
    assert pt.payload["content"] == content
    assert pt.payload["paper_id"] == paper_id
    assert pt.payload["visibility_generation"] == _VISIBILITY_GENERATION
