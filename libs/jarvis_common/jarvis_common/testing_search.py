"""Shared search/reranker test infrastructure: ``ScriptedReranker`` DI seam.

Cluster 9 of the 2026-05-24 polish-wave decomposition of ``jarvis_common.testing``.
"""

from __future__ import annotations

__all__ = ["ScriptedReranker"]

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ScriptedReranker — in-process DI seam replacing CrossEncoder (W0.3)
# ---------------------------------------------------------------------------


@dataclass
class ScriptedReranker:
    """In-process callable replacing CrossEncoder for test determinism.

    Provides two interfaces so it can be injected at either layer:

    1. **Async wrapper** (``rerank_chunks``) — drop-in for
       ``EmbeddingSearchMixin.rerank_chunks`` via ``app.state.reranker``::

           scripted = ScriptedReranker(scores=[0.9, 0.5, 0.1])
           with patch_app_state(app, {"reranker": scripted}):
               ...

    2. **Sync CrossEncoder interface** (``predict``) — for patching the
       underlying model via ``patch.object(reranker_module, "get_reranker",
       return_value=scripted)``.

    ``scores`` lists one float per chunk position.  Any chunk index beyond
    the list length is scored ``0.0`` rather than raising ``IndexError``.
    """

    scores: list[float] = field(default_factory=list)

    async def rerank_chunks(
        self,
        query: str,
        chunks: list[dict],
        top_k: int = 5,
    ) -> list[dict]:
        """Async wrapper mirroring ``EmbeddingSearchMixin.rerank_chunks``.

        Verified signature at search.py:189 (query, chunks: list[dict], top_k).
        Returns the top_k chunks sorted by their scripted score, descending.
        """
        ranked = sorted(
            range(len(chunks)),
            key=lambda i: self._score_at(i),
            reverse=True,
        )
        return [chunks[i] for i in ranked[:top_k]]

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Sync interface mirroring ``sentence_transformers.CrossEncoder.predict``.

        Returns one float per pair, in input order.
        """
        return [self._score_at(i) for i in range(len(pairs))]

    def _score_at(self, i: int) -> float:
        return self.scores[i] if 0 <= i < len(self.scores) else 0.0
