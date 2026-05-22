"""Char-tests for jarvis_common.testing factory helpers (Wave 1)."""

import inspect

import pytest
from jarvis_common.testing import ScriptedReranker, make_pool_and_conn, make_request
from jarvis_common.testing_embedder import _FakeEncoding, _make_embedder
from paper_ingestion.ingestion.embedder import Embedder


def test_make_request_sets_user_id_on_state() -> None:
    req = make_request(user_id=42)
    assert req.state.user_id == 42


def test_make_request_sets_role_when_provided() -> None:
    req = make_request(user_id=1, role="admin")
    assert req.state.user_role == "admin"


@pytest.mark.asyncio
async def test_make_pool_and_conn_raise_on_acquire() -> None:
    pool, _conn = make_pool_and_conn(raise_on_acquire=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        async with pool.acquire() as _:
            pass


@pytest.mark.asyncio
async def test_make_pool_and_conn_fetchrow_side_effects() -> None:
    pool, conn = make_pool_and_conn(fetchrow_side_effects=[{"r": 1}, {"r": 2}])
    assert await conn.fetchrow("any") == {"r": 1}
    assert await conn.fetchrow("any") == {"r": 2}


@pytest.mark.asyncio
async def test_testing_embedder_make_embedder_returns_embed_capable_mock() -> None:
    embedder = _make_embedder()
    # _make_embedder returns a real Embedder instance with mocked HTTP/Qdrant clients
    assert isinstance(embedder, Embedder)
    assert isinstance(embedder._encoding, _FakeEncoding)


# ---------------------------------------------------------------------------
# ScriptedReranker char-tests (W0.3)
# ---------------------------------------------------------------------------


def test_scripted_reranker_predict_sync_returns_per_pair_scores() -> None:
    """predict() returns one float per (query, passage) pair in input order."""
    scripted = ScriptedReranker(scores=[0.9, 0.5, 0.1])
    pairs = [("q", "passage-0"), ("q", "passage-1"), ("q", "passage-2")]
    result = scripted.predict(pairs)
    assert result == [0.9, 0.5, 0.1]


@pytest.mark.asyncio
async def test_scripted_reranker_async_rerank_chunks_returns_top_k_descending() -> None:
    """rerank_chunks returns at most top_k chunks, ordered by score descending."""
    scripted = ScriptedReranker(scores=[0.1, 0.9, 0.5])
    chunks = [
        {"content": "low-score chunk", "id": 0},
        {"content": "high-score chunk", "id": 1},
        {"content": "mid-score chunk", "id": 2},
    ]
    result = await scripted.rerank_chunks(query="test", chunks=chunks, top_k=2)
    assert len(result) == 2
    # highest-score chunk first
    assert result[0]["id"] == 1
    # second-highest next
    assert result[1]["id"] == 2


@pytest.mark.asyncio
async def test_scripted_reranker_missing_index_defaults_to_zero() -> None:
    """Chunks beyond the len(scores) list are scored 0.0, not IndexError."""
    scripted = ScriptedReranker(scores=[0.7])
    chunks = [
        {"content": "a", "id": 0},
        {"content": "b", "id": 1},  # no score provided
        {"content": "c", "id": 2},  # no score provided
    ]
    result = await scripted.rerank_chunks(query="q", chunks=chunks, top_k=3)
    # chunk 0 (score 0.7) first; extras get 0.0 (stable order within ties)
    assert result[0]["id"] == 0
    assert len(result) == 3


def test_scripted_reranker_async_signature_matches_embedder_protocol() -> None:
    """rerank_chunks method signature matches EmbeddingSearchMixin.rerank_chunks.

    Verified against services/paper_ingestion/paper_ingestion/ingestion/search.py:189.
    """
    from paper_ingestion.ingestion.search import EmbeddingSearchMixin

    real_sig = inspect.signature(EmbeddingSearchMixin.rerank_chunks)
    scripted_sig = inspect.signature(ScriptedReranker.rerank_chunks)

    # Both must accept: self, query: str, chunks: list, top_k: int
    real_params = list(real_sig.parameters.keys())
    scripted_params = list(scripted_sig.parameters.keys())
    assert real_params == scripted_params, (
        f"Parameter mismatch: real={real_params}, scripted={scripted_params}"
    )
    # Method must be a coroutine function (async def)
    assert inspect.iscoroutinefunction(ScriptedReranker.rerank_chunks)
