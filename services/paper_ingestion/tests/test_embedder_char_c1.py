"""C1 strict characterization snapshot — Embedder God-class decomposition.

Pins *exact* observable behavior (byte-for-byte chunk content, RRF ordering,
rerank ordering, discover ordering, embed_and_store Qdrant op sequence,
delete_paper_vectors selector) BEFORE the structural split and asserts it is
unchanged AFTER.  Loose membership/shape assertions live in
``test_embedder_behavior.py``; this file deliberately uses equality on the
regression-prone ordering/fusion code paths.

All external I/O (httpx, qdrant_client, asyncpg) is mocked.  These tests are a
refactor safety net, not integration tests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import via the canonical path AND the back-compat shim to prove BOTH public
# surfaces stay importable across the split.
from paper_ingestion.embedder import Embedder as ShimEmbedder
from paper_ingestion.ingestion.embedder import (
    _CHUNK_POINT_ID_NAMESPACE,
    CHUNK_TOKEN_LIMIT,
    COLLECTION_NAME,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_NAME,
    Embedder,
    EmbeddingBatchError,
    _point_payload,
    _user_scope_filter,
)
from paper_ingestion.models import ChunkForEmbedding

# D3-05: shared fake — replaces the local _FakeEncoding duplicated 3×.
from tests._embedder_fakes import _FakeEncoding


def _make_embedder() -> Embedder:
    # Uses paper_ingestion.ingestion.embedder.Embedder (direct path, not shim)
    # to validate both import surfaces stay in sync across the C3 split.
    e = Embedder(AsyncMock(), AsyncMock())
    e._encoding = _FakeEncoding()  # type: ignore[assignment]
    return e


# ---------------------------------------------------------------------------
# Public surface invariants
# ---------------------------------------------------------------------------


def test_public_surface_importable_from_both_paths():
    """Canonical and shim modules expose the same Embedder object."""
    assert Embedder is ShimEmbedder
    assert COLLECTION_NAME == "paper_chunks"
    assert CHUNK_TOKEN_LIMIT == 512
    assert _CHUNK_POINT_ID_NAMESPACE == uuid.NAMESPACE_DNS
    # Helper free functions remain importable from the canonical module.
    assert _point_payload(SimpleNamespace(payload={"k": 1})) == {"k": 1}
    assert _point_payload(SimpleNamespace(payload=None)) is None
    assert _user_scope_filter(None) is None
    scoped = _user_scope_filter(7)
    assert scoped is not None and scoped.should is not None


# ---------------------------------------------------------------------------
# chunk_text — exact boundary snapshot
# ---------------------------------------------------------------------------


def test_chunk_text_exact_boundary_snapshot():
    """Fixed multi-section input → exact chunk count, content, offsets, indices."""
    e = _make_embedder()
    # Three "## " sections; section 2 forced over the 512-char limit so it
    # sub-splits via the token-window force-split path (the trickiest branch).
    s1 = "## Intro\nshort intro paragraph."
    s2 = "## Big\n" + ("X" * (CHUNK_TOKEN_LIMIT + 40))
    s3 = "## End\nfinal short bit."
    text = "\n" + s1 + "\n" + s2 + "\n" + s3

    chunks = e.chunk_text(text)

    # Snapshot: indices contiguous, content stripped, every chunk within limit.
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert len(c.content) <= CHUNK_TOKEN_LIMIT
        assert c.content == c.content.strip()

    # Exact observed behavior pinned (captured against pre-refactor HEAD).
    # 4 chunks: intro, big-header window, force-split overlap window, end.
    assert len(chunks) == 4

    # Offsets track the raw (unstripped) text; content is .strip()'d, so
    # len(content) need not equal end_char - start_char.
    assert chunks[0].content == "## Intro\nshort intro paragraph."
    assert len(chunks[0].content) == 31
    assert (chunks[0].start_char, chunks[0].end_char) == (0, 32)

    # Chunk 1: "## Big\n" + 504 X's (len 511, just under the 512 limit).
    assert chunks[1].content == "## Big\n" + "X" * 504
    assert len(chunks[1].content) == 511
    assert (chunks[1].start_char, chunks[1].end_char) == (32, 544)

    # Chunk 2: token-window force-split tail, 98 X's, overlapped start_char=494.
    assert chunks[2].content == "X" * 98
    assert (chunks[2].start_char, chunks[2].end_char) == (494, 592)

    assert chunks[3].content == "## End\nfinal short bit."
    assert len(chunks[3].content) == 23
    assert (chunks[3].start_char, chunks[3].end_char) == (592, 616)


def test_chunk_text_empty_input_snapshot():
    e = _make_embedder()
    assert e.chunk_text("") == []
    assert e.chunk_text("   \n\n  ") == []


# ---------------------------------------------------------------------------
# embed_and_store — exact Qdrant op sequence + deterministic point IDs
# ---------------------------------------------------------------------------


async def test_embed_and_store_op_sequence_and_point_ids():
    e = _make_embedder()
    e.embed_texts = AsyncMock(
        side_effect=[
            [[0.1] * EMBEDDING_DIMENSION, [0.2] * EMBEDDING_DIMENSION],
            [[0.3] * EMBEDDING_DIMENSION],
        ]
    )
    chunks = [
        ChunkForEmbedding(chunk_index=i, content=f"c{i}", page_number=1, start_char=0, end_char=2)
        for i in range(3)
    ]

    point_ids = await e.embed_and_store(42, chunks, batch_size=2, user_id=7)

    # Deterministic uuid5(NAMESPACE_DNS, "paper_id:chunk_index"), order preserved.
    expected_ids = [str(uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, f"42:{i}")) for i in range(3)]
    assert point_ids == expected_ids

    # embed_texts called once per batch with the batch's contents, in order.
    assert e.embed_texts.await_args_list[0].args == (["c0", "c1"],)
    assert e.embed_texts.await_args_list[1].args == (["c2"],)

    # Two upsert calls (batch_size=2 over 3 chunks). Payload + ids exact.
    assert e.qdrant.upsert.await_count == 2
    first = e.qdrant.upsert.await_args_list[0].kwargs
    assert first["collection_name"] == COLLECTION_NAME
    p0 = first["points"][0]
    assert p0.id == expected_ids[0]
    assert p0.payload == {
        "paper_id": 42,
        "chunk_index": 0,
        "page_number": 1,
        "content": "c0",
        "embedding_model": EMBEDDING_MODEL_NAME,
        "user_id": 7,
    }


async def test_embed_and_store_raises_batch_error_after_partial_persist():
    e = _make_embedder()
    e.embed_texts = AsyncMock(side_effect=[[[0.1] * EMBEDDING_DIMENSION], RuntimeError("boom")])
    chunks = [
        ChunkForEmbedding(chunk_index=i, content=f"c{i}", page_number=1, start_char=0, end_char=2)
        for i in range(2)
    ]
    with pytest.raises(EmbeddingBatchError) as excinfo:
        await e.embed_and_store(9, chunks, batch_size=1)
    err = excinfo.value
    assert len(err.completed_chunks) == 1
    assert err.completed_point_ids == [str(uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, "9:0"))]


# ---------------------------------------------------------------------------
# hybrid_search — exact RRF ordering
# ---------------------------------------------------------------------------


async def test_hybrid_search_exact_rrf_ordering():
    """Fixed BM25 + semantic ranks → exact RRF-sorted paper id order + scores."""
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])

    def _row(pid: int, title: str):
        r = MagicMock()
        r.__getitem__ = lambda _s, k, _pid=pid, _t=title: {
            "id": _pid,
            "title": _t,
            "authors": ["A"],
            "url": "u",
            "abstract": "ab",
            "published_date": None,
        }[k]
        return r

    # BM25 order: 1, 2, 3  (ranks 1,2,3)
    bm25_rows = [_row(1, "p1"), _row(2, "p2"), _row(3, "p3")]
    conn = AsyncMock()
    conn.fetch.return_value = bm25_rows
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db_pool = MagicMock()
    db_pool.acquire.return_value = ctx

    # Semantic order by max chunk score: 3 (0.9) > 1 (0.7) > 4 (0.5)
    e.search_chunks_global = AsyncMock(
        return_value=[
            {"paper_id": 3, "score": 0.9, "content": "c", "chunk_index": 0},
            {"paper_id": 1, "score": 0.7, "content": "c", "chunk_index": 0},
            {"paper_id": 4, "score": 0.5, "content": "c", "chunk_index": 0},
        ]
    )

    results = await e.hybrid_search("q", db_pool=db_pool, limit=10, k=60)

    # Exact observed behavior pinned (captured against pre-refactor HEAD).
    # RRF: pid1 = 1/61 + 1/62; pid2 = 1/62; pid3 = 1/63 + 1/61; pid4 = 1/63.
    # Sorted by (-rrf, pid) → [1, 3, 2]. Paper 4 is semantic-only; with these
    # mocks its metadata round-trip yields no matching row so it is dropped
    # (paper-deleted-between-queries guard). This drop is the pinned invariant.
    k = 60
    rrf1 = 1 / (k + 1) + 1 / (k + 2)
    rrf3 = 1 / (k + 3) + 1 / (k + 1)
    rrf2 = 1 / (k + 2)
    assert [r["id"] for r in results] == [1, 3, 2]
    by_id = {r["id"]: r for r in results}
    assert by_id[1]["bm25_rank"] == 1
    assert by_id[1]["semantic_rank"] == 2
    assert by_id[3]["bm25_rank"] == 3
    assert by_id[3]["semantic_rank"] == 1
    assert by_id[2]["bm25_rank"] == 2
    assert by_id[2]["semantic_rank"] is None
    assert 4 not in by_id
    assert by_id[1]["rrf_score"] == round(rrf1, 8)
    assert by_id[3]["rrf_score"] == round(rrf3, 8)
    assert by_id[2]["rrf_score"] == round(rrf2, 8)


async def test_hybrid_search_offset_pagination_after_fusion():
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.5] * EMBEDDING_DIMENSION])

    def _row(pid: int):
        r = MagicMock()
        r.__getitem__ = lambda _s, k, _pid=pid: {
            "id": _pid,
            "title": f"t{_pid}",
            "authors": [],
            "url": "u",
            "abstract": "a",
            "published_date": None,
        }[k]
        return r

    conn = AsyncMock()
    conn.fetch.return_value = [_row(1), _row(2), _row(3)]
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db_pool = MagicMock()
    db_pool.acquire.return_value = ctx
    e.search_chunks_global = AsyncMock(return_value=[])

    page1 = await e.hybrid_search("q", db_pool=db_pool, limit=1, offset=0)
    page2 = await e.hybrid_search("q", db_pool=db_pool, limit=1, offset=1)
    assert page1[0]["id"] == 1
    assert page2[0]["id"] == 2


# ---------------------------------------------------------------------------
# rerank_chunks — exact reorder
# ---------------------------------------------------------------------------


async def test_rerank_chunks_exact_reorder(monkeypatch):
    e = _make_embedder()
    chunks = [
        {"content": "alpha", "chunk_index": 0},
        {"content": "beta", "chunk_index": 1},
        {"content": "gamma", "chunk_index": 2},
        {"content": "delta", "chunk_index": 3},
    ]

    class _FakeReranker:
        def rerank(self, query, passages, top_k):
            # Deterministically reverse-rank: last passage best.
            order = list(range(len(passages)))[::-1][:top_k]
            return [(idx, 1.0 - i * 0.1) for i, idx in enumerate(order)]

    monkeypatch.setattr(
        "jarvis_common.settings.get_reranker_settings",
        lambda: SimpleNamespace(reranker_backend="cross-encoder"),
    )
    monkeypatch.setattr("paper_ingestion.ingestion.reranker.get_reranker", lambda: _FakeReranker())

    out = await e.rerank_chunks("q", chunks, top_k=2)
    # Reranker reversed order, top_k=2 → indices [3, 2]
    assert [c["chunk_index"] for c in out] == [3, 2]


async def test_rerank_chunks_passthrough_when_small():
    e = _make_embedder()
    chunks = [{"content": "a"}, {"content": "b"}]
    out = await e.rerank_chunks("q", chunks, top_k=5)
    assert out == chunks


# ---------------------------------------------------------------------------
# search_chunks_global / search_similar — ordering preserved from Qdrant
# ---------------------------------------------------------------------------


async def test_search_chunks_global_preserves_qdrant_order():
    e = _make_embedder()
    e.embed_texts = AsyncMock(return_value=[[0.1] * EMBEDDING_DIMENSION])
    e.qdrant.query_points = AsyncMock(
        return_value=SimpleNamespace(
            points=[
                SimpleNamespace(
                    payload={
                        "paper_id": 5,
                        "chunk_index": 2,
                        "content": "first",
                        "page_number": 3,
                    },
                    score=0.91,
                ),
                SimpleNamespace(payload=None, score=0.99),  # skipped
                SimpleNamespace(
                    payload={
                        "paper_id": 6,
                        "chunk_index": 0,
                        "content": "second",
                        "page_number": 1,
                    },
                    score=0.42,
                ),
            ]
        )
    )
    out = await e.search_chunks_global("q", limit=10)
    assert out == [
        {
            "paper_id": 5,
            "chunk_index": 2,
            "content": "first",
            "page_number": 3,
            "score": 0.91,
        },
        {
            "paper_id": 6,
            "chunk_index": 0,
            "content": "second",
            "page_number": 1,
            "score": 0.42,
        },
    ]


# ---------------------------------------------------------------------------
# discover_from_seeds — exact dedup + ordering
# ---------------------------------------------------------------------------


async def test_discover_from_seeds_exact_dedup_and_order():
    e = _make_embedder()
    e.qdrant.scroll = AsyncMock(
        return_value=([SimpleNamespace(id="pt-1"), SimpleNamespace(id="pt-2")], None)
    )
    e.qdrant.query_points = AsyncMock(
        return_value=SimpleNamespace(
            points=[
                SimpleNamespace(payload={"paper_id": 10, "content": "lo"}, score=0.3),
                SimpleNamespace(payload={"paper_id": 11, "content": "hi"}, score=0.95),
                SimpleNamespace(payload={"paper_id": 10, "content": "best"}, score=0.8),
                SimpleNamespace(payload=None, score=0.99),
            ]
        )
    )
    db_pool = MagicMock()
    out = await e.discover_from_seeds([1], db_pool=db_pool, limit=5)
    # Dedup keeps best score per paper, sorted desc: 11 (0.95) then 10 (0.8)
    assert [(r["paper_id"], r["score"]) for r in out] == [(11, 0.95), (10, 0.8)]
    assert out[1]["content"] == "best"


# ---------------------------------------------------------------------------
# delete_paper_vectors — exact selector
# ---------------------------------------------------------------------------


async def test_delete_paper_vectors_selector():
    e = _make_embedder()
    await e.delete_paper_vectors(123)
    e.qdrant.delete.assert_awaited_once()
    kwargs = e.qdrant.delete.await_args.kwargs
    assert kwargs["collection_name"] == COLLECTION_NAME
    assert kwargs["wait"] is True
    cond = kwargs["points_selector"].must[0]
    assert cond.key == "paper_id"
    assert cond.match.value == 123
