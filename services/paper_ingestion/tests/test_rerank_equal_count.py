"""PI-CORR-02 — rerank must run when candidate count equals top_k.

``rerank_chunks`` short-circuits with ``if len(chunks) <= top_k: return
chunks[:top_k]``. At exactly ``len(chunks) == top_k`` that skips the reranker
entirely and returns the chunks in their *input* (vector-similarity) order
rather than the reranker's relevance order — a silent correctness bug. The
guard should be ``<`` so reranking still reorders an equal-count candidate set.

Only a real reranker (one that returns a non-identity ordering) distinguishes
the two: the assertion below would pass spuriously with an identity reranker,
so the stub reverses the order.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from paper_ingestion.ingestion.search import EmbeddingSearchMixin


class _ReversingReranker:
    """Stub reranker: returns (original_index, score) reversing the input order.

    Mirrors the real Reranker.rerank contract — list[(idx, score)] sorted by
    relevance, limited to top_k. Here "relevance" reverses input order so the
    output is observably different from the unranked passthrough.
    """

    def rerank(self, query, passages, top_k):
        n = len(passages)
        ranked = [(i, float(n - i)) for i in reversed(range(n))]
        return ranked[:top_k]


@pytest.mark.asyncio
async def test_rerank_runs_when_count_equals_top_k():
    """len(chunks) == top_k must still invoke the reranker and reorder.

    chunks are [c0, c1, c2] with top_k=3. The reversing reranker must produce
    [c2, c1, c0]. With the buggy ``<=`` guard the reranker never runs and the
    result stays [c0, c1, c2].
    """
    instance = object.__new__(EmbeddingSearchMixin)
    chunks = [
        {"content": "alpha", "chunk_index": 0},
        {"content": "beta", "chunk_index": 1},
        {"content": "gamma", "chunk_index": 2},
    ]
    top_k = len(chunks)  # equal-count boundary

    with (
        patch(
            "jarvis_common.settings.get_reranker_settings",
            return_value=SimpleNamespace(reranker_backend="cross-encoder"),
        ),
        patch(
            "paper_ingestion.ingestion.reranker.get_reranker",
            return_value=_ReversingReranker(),
        ),
    ):
        result = await instance.rerank_chunks("query", chunks, top_k=top_k)

    assert [c["chunk_index"] for c in result] == [2, 1, 0], (
        "rerank_chunks must reorder via the reranker even at len(chunks)==top_k; "
        f"got order {[c['chunk_index'] for c in result]}"
    )
