"""Tests for re-embedding pipeline (T2-1).

Covers:
1. Old Qdrant points deleted after new ones stored
2. DB embedding_model column updated after re-embedding
3. Idempotent: already-reembedded papers are skipped
4. Partial embedding failure preserves old Qdrant points (atomic safety)
5. Embedder payload includes embedding_model in Qdrant points
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# scripts/ lives at the repo root, which is not in pytest's pythonpath.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_JARVIS_COMMON_ROOT = str(Path(_PROJECT_ROOT) / "libs" / "jarvis_common")
if _JARVIS_COMMON_ROOT not in sys.path:
    sys.path.insert(0, _JARVIS_COMMON_ROOT)


class _FakePointIdsList:
    def __init__(self, *, points):
        self.points = points


class _FakePointStruct:
    def __init__(self, *, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload


class _FakeVectorParams:
    def __init__(self, *, size, distance):
        self.size = size
        self.distance = distance


# We need precise qdrant_client.models stubs (real classes with .points attribute)
# so the embedder code behaves predictably. Override per-test via autouse fixture.

_fake_qdrant_client_mod = SimpleNamespace(AsyncQdrantClient=MagicMock())
_fake_qdrant_models_mod = SimpleNamespace(
    Distance=SimpleNamespace(COSINE="cosine"),
    FieldCondition=MagicMock,
    Filter=MagicMock,
    MatchValue=MagicMock,
    PointIdsList=_FakePointIdsList,
    PointStruct=_FakePointStruct,
    VectorParams=_FakeVectorParams,
)


@pytest.fixture(autouse=True)
def _install_reembed_stubs(monkeypatch):
    """Scope precise qdrant_client stubs to each test only.

    Also evicts paper_ingestion.embedder so test_embedder_payload_includes_model_name
    always re-imports the real Embedder class (not a MagicMock from another test file).
    """
    monkeypatch.setitem(sys.modules, "qdrant_client", _fake_qdrant_client_mod)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", _fake_qdrant_models_mod)
    monkeypatch.delitem(sys.modules, "paper_ingestion.embedder", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk_row(
    paper_id: int,
    chunk_index: int,
    content: str = "chunk text",
    embedding_id: str | None = None,
    embedding_model: str | None = None,
) -> MagicMock:
    """Create a mock asyncpg.Record for a paper_chunks row."""
    row_data = {
        "id": chunk_index + 100,  # DB row id
        "paper_id": paper_id,
        "chunk_index": chunk_index,
        "content": content,
        "page_number": 1,
        "start_char": 0,
        "end_char": len(content),
        "embedding_id": embedding_id or str(uuid.uuid4()),
        "embedding_model": embedding_model,
    }
    rec = MagicMock()
    rec.__getitem__ = lambda self, key: row_data[key]
    rec.keys = lambda: row_data.keys()
    rec.get = lambda key, default=None: row_data.get(key, default)
    return rec


def _make_mock_pool(
    fetch_return: list | None = None,
    fetchval_return=None,
) -> AsyncMock:
    """Create a mock asyncpg.Pool with configurable fetch results."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()

    # transaction context manager
    txn = AsyncMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_sequenced_pool(
    fetch_returns: list[list],
    *,
    fetchval_returns: list[int] | None = None,
) -> tuple[AsyncMock, list[AsyncMock]]:
    """Create a pool whose acquired connections return query-specific results in order."""
    fetchval_returns = list(fetchval_returns or [])
    conns: list[AsyncMock] = []
    contexts: list[AsyncMock] = []
    for fetch_return in fetch_returns:
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=fetch_return)
        conn.fetchval = AsyncMock(side_effect=fetchval_returns)
        conn.execute = AsyncMock()
        conn.executemany = AsyncMock()

        txn = AsyncMock()
        txn.__aenter__ = AsyncMock(return_value=txn)
        txn.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=txn)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        contexts.append(ctx)
        conns.append(conn)

    pool = AsyncMock()
    pool.acquire = MagicMock(side_effect=contexts)
    pool.close = AsyncMock()
    return pool, conns


class _FakeEmbeddingBackend:
    name = "fake"

    def __init__(
        self,
        *,
        embedding_dim: int,
        fail_on_call: int | None = None,
        wrong_count: bool = False,
        wrong_dimension: bool = False,
    ) -> None:
        self.embedding_dim = embedding_dim
        self.fail_on_call = fail_on_call
        self.wrong_count = wrong_count
        self.wrong_dimension = wrong_dimension
        self.calls = 0
        self.seen_batches: list[list[str]] = []

    async def embed_texts(self, _client, texts):
        self.calls += 1
        self.seen_batches.append(list(texts))
        if self.fail_on_call == self.calls:
            raise RuntimeError(f"Embedding API failure on batch {self.calls}")
        dim = self.embedding_dim - 1 if self.wrong_dimension else self.embedding_dim
        count = max(0, len(texts) - 1) if self.wrong_count else len(texts)
        return [[0.1] * dim for _ in range(count)]


def _make_mock_http_client(embedding_dim: int = 1024) -> AsyncMock:
    """Create a mock httpx.AsyncClient that returns fake embeddings."""
    client = AsyncMock()

    async def _post(url, **kwargs):
        texts = kwargs.get("json", {}).get("input", [])
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={
                "data": [
                    {"index": i, "embedding": [0.1] * embedding_dim} for i in range(len(texts))
                ]
            }
        )
        return resp

    client.post = _post
    return client


def _collection_info(size: int) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=size)))
    )


# ---------------------------------------------------------------------------
# Test 1: Old Qdrant points deleted before new ones stored
# ---------------------------------------------------------------------------


async def test_ensure_collection_dimension_creates_missing_collection():
    """Missing Qdrant collection is created at the configured embedding dimension."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=False)

    await reembed_mod.ensure_collection_dimension(qdrant)

    qdrant.create_collection.assert_awaited_once()
    vector_config = qdrant.create_collection.await_args.kwargs["vectors_config"]
    assert vector_config.size == reembed_mod.EMBEDDING_DIMENSION


async def test_ensure_collection_dimension_rejects_model_dimension_env_mismatch():
    """Known embedding model and EMBEDDING_DIMENSION drift fails before Qdrant mutation."""
    import importlib

    with patch.dict(
        "os.environ",
        {"EMBEDDING_MODEL_NAME": "qwen3-embedding:0.6b", "EMBEDDING_DIMENSION": "768"},
    ):
        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=False)

    with pytest.raises(RuntimeError, match="Embedding configuration mismatch"):
        await reembed_mod.ensure_collection_dimension(qdrant)

    qdrant.collection_exists.assert_not_called()
    qdrant.create_collection.assert_not_called()


async def test_ensure_collection_dimension_refuses_mismatch_without_checkpoint_flag():
    """Wrong-dimension collection is a hard stop unless recreate is explicit."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.get_collection = AsyncMock(
        return_value=_collection_info(reembed_mod.EMBEDDING_DIMENSION - 256)
    )

    with pytest.raises(reembed_mod.ScriptError, match="REEMBED_RECREATE_COLLECTION=true"):
        await reembed_mod.ensure_collection_dimension(qdrant)

    qdrant.delete_collection.assert_not_called()
    qdrant.create_collection.assert_not_called()


async def test_ensure_collection_dimension_recreates_mismatch_with_checkpoint_flag():
    """The deliberate recreate flag deletes and recreates the wrong-dimension collection."""
    sys_path_ctx = patch.dict(
        "os.environ", {"REEMBED_RECREATE_COLLECTION": "true", "REEMBED_SNAPSHOT_CONFIRMED": "true"}
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.get_collection = AsyncMock(
        return_value=_collection_info(reembed_mod.EMBEDDING_DIMENSION - 256)
    )

    await reembed_mod.ensure_collection_dimension(qdrant)

    qdrant.delete_collection.assert_awaited_once_with(collection_name="paper_chunks")
    qdrant.create_collection.assert_awaited_once()


async def test_old_qdrant_points_deleted():
    """When re-embedding a paper, old Qdrant points are deleted first."""
    # Import here to allow patching
    sys_path_ctx = patch.dict(
        "os.environ",
        {
            "LITELLM_BASE_URL": "http://test:4000",
            "EMBEDDING_MODEL_NAME": "nomic-embed-text",
            "EMBEDDING_DIMENSION": "768",
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    old_embedding_ids = [f"old-uuid-{i}" for i in range(3)]
    chunks = [
        _make_chunk_row(
            1,
            i,
            f"chunk {i}",
            embedding_id=old_embedding_ids[i],
            embedding_model="qwen3-embedding:0.6b",
        )
        for i in range(3)
    ]

    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    http_client = _make_mock_http_client(reembed_mod.EMBEDDING_DIMENSION)
    backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)

    count = await reembed_mod.reembed_paper(1, pool, qdrant, http_client, backend)

    assert count == 3

    # Verify old points were deleted
    qdrant.delete.assert_called_once()
    delete_call = qdrant.delete.call_args
    assert delete_call.kwargs["collection_name"] == "paper_chunks"
    assert delete_call.kwargs["points_selector"].points == old_embedding_ids

    # Verify new points were upserted
    qdrant.upsert.assert_called()
    upsert_call = qdrant.upsert.call_args
    new_points = upsert_call.kwargs["points"]
    assert len(new_points) == 3
    # New points should have the new model name in payload
    for pt in new_points:
        assert pt.payload["embedding_model"] == "nomic-embed-text"
        assert pt.payload["paper_id"] == 1

    assert [pt.id for pt in new_points] == [
        reembed_mod.deterministic_point_id(1, i, "nomic-embed-text") for i in range(3)
    ]


async def test_old_qdrant_cleanup_keeps_existing_target_point_ids():
    """Mixed-model papers must not delete chunks already using target point ids."""
    sys_path_ctx = patch.dict(
        "os.environ",
        {
            "LITELLM_BASE_URL": "http://test:4000",
            "EMBEDDING_MODEL_NAME": "nomic-embed-text",
            "EMBEDDING_DIMENSION": "768",
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    already_target_id = reembed_mod.deterministic_point_id(1, 0, "nomic-embed-text")
    stale_id = "old-stale-point"
    chunks = [
        _make_chunk_row(
            1,
            0,
            "already target",
            embedding_id=already_target_id,
            embedding_model="nomic-embed-text",
        ),
        _make_chunk_row(
            1,
            1,
            "stale chunk",
            embedding_id=stale_id,
            embedding_model="qwen3-embedding:0.6b",
        ),
    ]
    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)

    await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock(), backend)

    qdrant.delete.assert_awaited_once()
    deleted_ids = qdrant.delete.await_args.kwargs["points_selector"].points
    assert deleted_ids == [stale_id]


# ---------------------------------------------------------------------------
# Test 2: DB updated with new embedding_model after re-embedding
# ---------------------------------------------------------------------------


async def test_db_embedding_model_updated():
    """After re-embedding, embedding_model column is updated to new model name."""
    sys_path_ctx = patch.dict(
        "os.environ",
        {
            "LITELLM_BASE_URL": "http://test:4000",
            "EMBEDDING_MODEL_NAME": "nomic-embed-text",
            "EMBEDDING_DIMENSION": "768",
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    chunks = [_make_chunk_row(42, i, f"chunk {i}", embedding_model=None) for i in range(2)]

    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    http_client = _make_mock_http_client(reembed_mod.EMBEDDING_DIMENSION)
    backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)

    await reembed_mod.reembed_paper(42, pool, qdrant, http_client, backend)

    # Get the connection mock from the second acquire() call (the UPDATE path)
    # The pool.acquire() is called twice: once for fetch, once for update
    assert pool.acquire.call_count == 2

    # The second acquire context manager's __aenter__ returns the conn mock
    second_ctx = pool.acquire.return_value
    conn = await second_ctx.__aenter__()

    # Verify batched UPDATE sets the new model name
    conn.executemany.assert_awaited_once()
    sql, values = conn.executemany.await_args.args
    assert "UPDATE paper_chunks" in sql
    assert len(values) == 2
    for point_id, model_name, row_id in values:
        assert model_name == "nomic-embed-text"
        assert row_id in {100, 101}
        assert point_id in {
            reembed_mod.deterministic_point_id(42, 0, "nomic-embed-text"),
            reembed_mod.deterministic_point_id(42, 1, "nomic-embed-text"),
        }


# ---------------------------------------------------------------------------
# Test 3: Idempotent - papers already embedded with target model are skipped
# ---------------------------------------------------------------------------


async def test_idempotent_skip_already_reembedded():
    """Running re-embed when all papers are already done results in no work."""
    sys_path_ctx = patch.dict(
        "os.environ",
        {
            "LITELLM_BASE_URL": "http://test:4000",
            "EMBEDDING_MODEL_NAME": "nomic-embed-text",
            "EMBEDDING_DIMENSION": "768",
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    # Mock the pool for main() — the initial query returns no papers
    pool = AsyncMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])  # no papers need re-embedding

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    pool.close = AsyncMock()

    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.get_collection = AsyncMock(
        return_value=_collection_info(reembed_mod.EMBEDDING_DIMENSION)
    )

    # create_pool is an async function, so the mock must return a coroutine
    async def _fake_create_pool(*args, **kwargs):
        return pool

    with (
        patch.object(reembed_mod.asyncpg, "create_pool", side_effect=_fake_create_pool),
        patch.object(reembed_mod, "AsyncQdrantClient", return_value=qdrant),
    ):
        await reembed_mod.main()

    # Pool was used only for the initial query, then closed
    pool.close.assert_called_once()

    # No reembed_paper calls should have happened — verify no Qdrant ops
    qdrant.delete.assert_not_called()
    qdrant.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Pool creation failure exits cleanly
# ---------------------------------------------------------------------------


async def test_main_exits_when_pool_creation_fails():
    """main should exit with status 1 when the database pool cannot be created."""
    sys_path_ctx = patch.dict(
        "os.environ",
        {
            "LITELLM_BASE_URL": "http://test:4000",
            "EMBEDDING_MODEL_NAME": "nomic-embed-text",
            "EMBEDDING_DIMENSION": "768",
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    with patch.object(reembed_mod.asyncpg, "create_pool", AsyncMock(return_value=None)):
        with pytest.raises(reembed_mod.ScriptError, match="Failed to create database pool"):
            await reembed_mod.main()


# ---------------------------------------------------------------------------
# Test 5: Partial embedding failure preserves old Qdrant points
# ---------------------------------------------------------------------------


async def test_reembed_partial_failure_preserves_old_points():
    """If embed_texts raises on the 2nd call, old Qdrant points are NOT deleted."""
    sys_path_ctx = patch.dict(
        "os.environ",
        {
            "LITELLM_BASE_URL": "http://test:4000",
            "EMBEDDING_MODEL_NAME": "nomic-embed-text",
            "EMBEDDING_DIMENSION": "768",
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    # Create enough chunks to require 2 batches (EMBED_BATCH_SIZE = 32)
    old_embedding_ids = [f"old-uuid-{i}" for i in range(40)]
    chunks = [
        _make_chunk_row(
            1,
            i,
            f"chunk {i}",
            embedding_id=old_embedding_ids[i],
            embedding_model="qwen3-embedding:0.6b",
        )
        for i in range(40)
    ]

    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()

    backend = _FakeEmbeddingBackend(
        embedding_dim=reembed_mod.EMBEDDING_DIMENSION,
        fail_on_call=2,
    )

    with pytest.raises(RuntimeError, match="Embedding API failure on batch 2"):
        await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock(), backend)

    # Old points must NOT have been deleted (atomic safety)
    qdrant.delete.assert_not_called()


async def test_main_raises_after_non_script_error_paper_failures():
    """Per-paper non-ScriptError failures make the overall run fail after the loop."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    paper_ids = [{"paper_id": 1}, {"paper_id": 2}]
    pool, _conns = _make_sequenced_pool([paper_ids])
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.get_collection = AsyncMock(
        return_value=_collection_info(reembed_mod.EMBEDDING_DIMENSION)
    )

    async def _fake_create_pool(*args, **kwargs):
        return pool

    reembed_calls: list[int] = []

    async def _fake_reembed_paper(pid, *_args):
        reembed_calls.append(pid)
        if pid == 1:
            raise RuntimeError("paper exploded")
        return 3

    fake_backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)
    with (
        patch.object(reembed_mod.asyncpg, "create_pool", side_effect=_fake_create_pool),
        patch.object(reembed_mod, "AsyncQdrantClient", return_value=qdrant),
        patch.object(reembed_mod, "build_embedding_backend", return_value=fake_backend),
        patch.object(reembed_mod, "reembed_paper", side_effect=_fake_reembed_paper),
    ):
        with pytest.raises(reembed_mod.ScriptError, match="paper_id=1"):
            await reembed_mod.main()

    assert reembed_calls == [1, 2]
    pool.close.assert_awaited_once()


async def test_reembed_old_qdrant_delete_failure_is_fatal_by_default():
    """Old Qdrant cleanup failures are fatal but keep DB rows retryable."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    chunks = [_make_chunk_row(1, 0, "chunk", embedding_id="old-point")]
    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    qdrant.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)

    with pytest.raises(reembed_mod.ScriptError, match="Failed to delete old Qdrant points"):
        await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock(), backend)

    conn = await pool.acquire.return_value.__aenter__()
    conn.executemany.assert_not_awaited()


async def test_reembed_old_qdrant_delete_failure_can_continue_with_explicit_flag():
    """Debug continuation mode preserves the old tolerant cleanup behavior explicitly."""
    with patch.dict("os.environ", {"REEMBED_CONTINUE_ON_ERROR": "true"}):
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    chunks = [_make_chunk_row(1, 0, "chunk", embedding_id="old-point")]
    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    qdrant.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)

    assert await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock(), backend) == 1


async def test_reembed_qdrant_writes_wait_for_completion_before_db_update():
    """Qdrant upsert/delete writes wait so final DB/Qdrant parity cannot race."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    chunks = [_make_chunk_row(1, 0, "chunk", embedding_id="old-point")]
    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)

    await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock(), backend)

    qdrant.upsert.assert_awaited_once()
    qdrant.delete.assert_awaited_once()
    assert qdrant.upsert.await_args.kwargs["wait"] is True
    assert qdrant.delete.await_args.kwargs["wait"] is True


async def test_verify_postconditions_requires_db_target_count_and_qdrant_count_parity():
    """Final verification fails when target-model DB chunks and Qdrant vectors diverge."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    pool = _make_mock_pool(fetchval_return=7)
    qdrant = AsyncMock()
    qdrant.count = AsyncMock(return_value=SimpleNamespace(count=8))

    with pytest.raises(reembed_mod.ScriptError, match="Postcondition failed"):
        await reembed_mod.verify_postconditions(pool, qdrant)


async def test_main_runs_postcondition_after_successful_reembed():
    """Successful re-embed runs the final DB/Qdrant count parity gate."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    paper_ids = [{"paper_id": 1}]
    pool, _conns = _make_sequenced_pool([paper_ids])
    qdrant = AsyncMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.get_collection = AsyncMock(
        return_value=_collection_info(reembed_mod.EMBEDDING_DIMENSION)
    )

    async def _fake_create_pool(*args, **kwargs):
        return pool

    fake_backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)
    with (
        patch.object(reembed_mod.asyncpg, "create_pool", side_effect=_fake_create_pool),
        patch.object(reembed_mod, "AsyncQdrantClient", return_value=qdrant),
        patch.object(reembed_mod, "build_embedding_backend", return_value=fake_backend),
        patch.object(reembed_mod, "reembed_paper", AsyncMock(return_value=3)),
        patch.object(reembed_mod, "verify_postconditions", AsyncMock()) as verify_postconditions,
    ):
        await reembed_mod.main()

    verify_postconditions.assert_awaited_once_with(pool, qdrant)


async def test_reembed_fails_on_embedding_count_mismatch():
    """Partial embedding responses fail loudly before Qdrant writes."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    chunks = [_make_chunk_row(1, i, f"chunk {i}") for i in range(2)]
    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    backend = _FakeEmbeddingBackend(
        embedding_dim=reembed_mod.EMBEDDING_DIMENSION,
        wrong_count=True,
    )

    with pytest.raises(reembed_mod.ScriptError, match="Embedding count mismatch"):
        await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock(), backend)

    qdrant.upsert.assert_not_called()


async def test_reembed_fails_on_embedding_dimension_mismatch():
    """Wrong-size embeddings fail before Qdrant upsert/DB update."""
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    chunks = [_make_chunk_row(1, 0, "chunk")]
    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    backend = _FakeEmbeddingBackend(
        embedding_dim=reembed_mod.EMBEDDING_DIMENSION,
        wrong_dimension=True,
    )

    with pytest.raises(reembed_mod.ScriptError, match="Embedding dimension mismatch"):
        await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock(), backend)

    qdrant.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Embedder payload includes embedding_model in Qdrant points
# ---------------------------------------------------------------------------


async def test_embedder_payload_includes_model_name():
    """embed_and_store includes embedding_model in Qdrant point payloads."""
    # _install_reembed_stubs autouse fixture already evicted paper_ingestion.embedder.
    from paper_ingestion.ingestion.embedder import EMBEDDING_MODEL_NAME, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    # Mock embed_texts to return fake embeddings
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION

    embedder.embed_texts = AsyncMock(
        return_value=[[0.1] * EMBEDDING_DIMENSION, [0.2] * EMBEDDING_DIMENSION]
    )

    chunks = [
        ChunkForEmbedding(
            chunk_index=0, content="hello world", page_number=1, start_char=0, end_char=11
        ),
        ChunkForEmbedding(
            chunk_index=1, content="foo bar", page_number=1, start_char=11, end_char=18
        ),
    ]

    await embedder.embed_and_store(paper_id=99, chunks=chunks)

    mock_qdrant.upsert.assert_called_once()
    upserted_points = mock_qdrant.upsert.call_args.kwargs["points"]
    assert len(upserted_points) == 2
    for pt in upserted_points:
        assert pt.payload["embedding_model"] == EMBEDDING_MODEL_NAME
        assert pt.payload["paper_id"] == 99


def test_build_embedding_backend_accepts_supported_names():
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)

    assert reembed_mod.build_embedding_backend("litellm").name == "litellm"
    assert reembed_mod.build_embedding_backend("local").name == "local"


def test_build_embedding_backend_rejects_unknown_name():
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)

    with pytest.raises(reembed_mod.ScriptError, match="Unsupported REEMBED_BACKEND"):
        reembed_mod.build_embedding_backend("bogus")


def test_parse_args_supports_safe_help_flags():
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)

    args = reembed_mod.parse_args(["--benchmark", "--backend", "local", "--benchmark-size", "2"])

    assert args.benchmark is True
    assert args.backend == "local"
    assert args.benchmark_size == 2


def test_deterministic_point_id_is_stable_and_model_scoped():
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)

    first = reembed_mod.deterministic_point_id(1, 2, "qwen3-embedding:0.6b")
    second = reembed_mod.deterministic_point_id(1, 2, "qwen3-embedding:0.6b")
    other_model = reembed_mod.deterministic_point_id(1, 2, "nomic-embed-text")

    assert first == second
    assert first != other_model
    assert len(first) == 36


async def test_run_benchmark_is_read_only():
    import importlib

    import scripts.reembed as reembed_mod

    importlib.reload(reembed_mod)
    rows = [_make_chunk_row(1, i, f"chunk {i}") for i in range(3)]
    pool = _make_mock_pool(fetch_return=rows)
    backend = _FakeEmbeddingBackend(embedding_dim=reembed_mod.EMBEDDING_DIMENSION)

    await reembed_mod.run_benchmark(pool, backend)

    ctx = pool.acquire.return_value
    conn = await ctx.__aenter__()
    conn.fetch.assert_awaited_once()
    conn.executemany.assert_not_called()
    conn.execute.assert_not_called()
