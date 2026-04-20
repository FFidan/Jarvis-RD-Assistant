"""Tests for re-embedding pipeline (T2-1).

Covers:
1. Old Qdrant points deleted after new ones stored
2. DB embedding_model column updated after re-embedding
3. Idempotent: already-reembedded papers are skipped
4. Partial embedding failure preserves old Qdrant points (atomic safety)
5. Embedder payload includes embedding_model in Qdrant points
"""

from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path so we can import scripts.reembed
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_SERVICE_ROOT = os.path.join(_PROJECT_ROOT, "services", "paper_ingestion")
if _SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _SERVICE_ROOT)
_SCRIPTS_ROOT = os.path.join(_PROJECT_ROOT, "scripts")
if _SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, _SCRIPTS_ROOT)


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


if "qdrant_client" not in sys.modules:
    sys.modules["qdrant_client"] = SimpleNamespace(AsyncQdrantClient=MagicMock())

if "qdrant_client.models" not in sys.modules:
    sys.modules["qdrant_client.models"] = SimpleNamespace(
        Distance=SimpleNamespace(COSINE="cosine"),
        PointIdsList=_FakePointIdsList,
        PointStruct=_FakePointStruct,
        VectorParams=_FakeVectorParams,
    )

if "tiktoken" not in sys.modules:
    fake_tiktoken = MagicMock()
    fake_encoding = MagicMock()
    fake_encoding.encode.return_value = [1, 2, 3]
    fake_encoding.decode.return_value = "chunk text"
    fake_tiktoken.get_encoding.return_value = fake_encoding
    sys.modules["tiktoken"] = fake_tiktoken

if getattr(sys.modules.get("paper_ingestion.embedder"), "Embedder", None) is object:
    del sys.modules["paper_ingestion.embedder"]


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


def _make_mock_http_client(embedding_dim: int = 768) -> AsyncMock:
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


# ---------------------------------------------------------------------------
# Test 1: Old Qdrant points deleted before new ones stored
# ---------------------------------------------------------------------------


async def test_old_qdrant_points_deleted():
    """When re-embedding a paper, old Qdrant points are deleted first."""
    # Import here to allow patching
    sys_path_ctx = patch.dict(
        "os.environ",
        {
            "LITELLM_BASE_URL": "http://test:4000",
            "EMBEDDING_MODEL_NAME": "nomic-embed-text",
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
    http_client = _make_mock_http_client()

    count = await reembed_mod.reembed_paper(1, pool, qdrant, http_client)

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
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    chunks = [_make_chunk_row(42, i, f"chunk {i}", embedding_model=None) for i in range(2)]

    pool = _make_mock_pool(fetch_return=chunks)
    qdrant = AsyncMock()
    http_client = _make_mock_http_client()

    await reembed_mod.reembed_paper(42, pool, qdrant, http_client)

    # Get the connection mock from the second acquire() call (the UPDATE path)
    # The pool.acquire() is called twice: once for fetch, once for update
    assert pool.acquire.call_count == 2

    # The second acquire context manager's __aenter__ returns the conn mock
    second_ctx = pool.acquire.return_value
    conn = await second_ctx.__aenter__()

    # Verify UPDATE calls set the new model name
    assert conn.execute.call_count == 2  # one per chunk
    for call in conn.execute.call_args_list:
        args = call.args
        # args[0] is the SQL, args[2] is the embedding_model value
        assert "UPDATE paper_chunks" in args[0]
        assert args[2] == "nomic-embed-text"


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
        },
    )
    with sys_path_ctx:
        import importlib

        import scripts.reembed as reembed_mod

        importlib.reload(reembed_mod)

    with patch.object(reembed_mod.asyncpg, "create_pool", AsyncMock(return_value=None)):
        with pytest.raises(SystemExit) as exc_info:
            await reembed_mod.main()

    assert exc_info.value.code == 1


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

    # embed_texts succeeds on first batch, raises on second
    call_count = 0

    async def _mock_embed_texts(client, texts):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [[0.1] * 768 for _ in texts]
        raise RuntimeError("Embedding API failure on batch 2")

    with patch.object(reembed_mod, "embed_texts", side_effect=_mock_embed_texts):
        with pytest.raises(RuntimeError, match="Embedding API failure on batch 2"):
            await reembed_mod.reembed_paper(1, pool, qdrant, AsyncMock())

    # Old points must NOT have been deleted (atomic safety)
    qdrant.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Embedder payload includes embedding_model in Qdrant points
# ---------------------------------------------------------------------------


async def test_embedder_payload_includes_model_name():
    """embed_and_store includes embedding_model in Qdrant point payloads."""
    if getattr(sys.modules.get("paper_ingestion.embedder"), "Embedder", None) is object:
        del sys.modules["paper_ingestion.embedder"]

    from paper_ingestion.embedder import EMBEDDING_MODEL_NAME, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    embedder = Embedder(mock_http, mock_qdrant)

    # Mock embed_texts to return fake embeddings
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 768, [0.2] * 768])

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
