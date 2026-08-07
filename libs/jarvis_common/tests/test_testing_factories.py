"""Char-tests for jarvis_common.testing factory helpers."""

import inspect

import pytest
from jarvis_common.testing import ScriptedReranker, make_pool_and_conn, make_request
from jarvis_common.testing_db import make_conn, make_multi_acquire_pool


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
async def test_make_pool_and_conn_execute_return() -> None:
    pool, _conn = make_pool_and_conn(execute_return="status-tag-42")
    async with pool.acquire() as conn:
        assert await conn.execute("any") == "status-tag-42"


@pytest.mark.asyncio
async def test_multi_acquire_pool_yields_distinct_connections() -> None:
    """Successive acquire() must yield different conns, not the same one twice."""
    pool, (c1, c2) = make_multi_acquire_pool(2)
    async with pool.acquire() as a, pool.acquire() as b:
        assert a is not b
        assert a is c1
        assert b is c2


@pytest.mark.asyncio
async def test_multi_acquire_pool_uses_prebuilt_conns_in_order() -> None:
    """Pre-built conns are yielded as-is, in the order supplied."""
    first = make_conn(fetchval_return=1)
    second = make_conn(fetchval_return=2)
    pool, _conns = make_multi_acquire_pool([first, second])
    async with pool.acquire() as a:
        assert await a.fetchval("q") == 1
    async with pool.acquire() as b:
        assert await b.fetchval("q") == 2


@pytest.mark.asyncio
async def test_multi_acquire_pool_await_acquire_mode() -> None:
    """await pool.acquire() yields the conn directly; pool.release is awaitable."""
    pool, (conn,) = make_multi_acquire_pool(1, await_acquire=True)
    acquired = await pool.acquire()
    assert acquired is conn
    await pool.release(acquired)
    pool.release.assert_awaited_once_with(conn)


@pytest.mark.asyncio
async def test_direct_methods_exposes_conn_query_methods_on_pool() -> None:
    """direct_methods=True serves pool-level calls from the conn's mocks."""
    pool, conn = make_pool_and_conn(fetchrow_return={"user_id": 7}, direct_methods=True)
    assert await pool.fetchrow("q") == {"user_id": 7}
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_methods_off_pool_methods_not_awaitable() -> None:
    """Without the flag, pool.fetchrow stays a plain MagicMock (not awaitable)."""
    pool, _conn = make_pool_and_conn(fetchrow_return={"user_id": 7})
    with pytest.raises(TypeError):
        await pool.fetchrow("q")


@pytest.mark.asyncio
async def test_direct_methods_sees_fetchrow_side_effects() -> None:
    """Pool-level fetchrow must reflect fetchrow_side_effects, which replaces
    conn.fetchrow — guards the wiring order inside the factory."""
    pool, _conn = make_pool_and_conn(fetchrow_side_effects=[{"r": 1}], direct_methods=True)
    assert await pool.fetchrow("q") == {"r": 1}


# ---------------------------------------------------------------------------
# ScriptedReranker char-tests
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
