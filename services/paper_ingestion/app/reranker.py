"""Cross-encoder reranker for improving retrieval quality.

Re-scores query-passage pairs using a cross-encoder model to produce
more accurate relevance rankings than bi-encoder similarity alone.
"""

import logging

logger = logging.getLogger(__name__)


class Reranker:
    """Cross-encoder reranker using sentence-transformers.

    Parameters
    ----------
    model_name : str
        HuggingFace model name for the cross-encoder.
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self._model_name = model_name
        self._model = None

    def _load_model_if_needed(self) -> None:
        """Lazy-load the cross-encoder model on first use."""
        if self._model is not None:
            return
        from sentence_transformers import CrossEncoder

        try:
            self._model = CrossEncoder(self._model_name, device="cuda")
            logger.info("Cross-encoder loaded on CUDA: %s", self._model_name)
        except Exception:
            logger.warning("CUDA unavailable for cross-encoder, falling back to CPU")
            self._model = CrossEncoder(self._model_name, device="cpu")
            logger.info("Cross-encoder loaded on CPU: %s", self._model_name)

    def rerank(
        self,
        query: str,
        passages: list[str],
        top_k: int = 5,
    ) -> list[tuple[int, float]]:
        """Re-score and rerank passages using the cross-encoder.

        Parameters
        ----------
        query : str
            The search query.
        passages : list[str]
            List of passage texts to rerank.
        top_k : int
            Number of top results to return.

        Returns
        -------
        list[tuple[int, float]]
            List of (original_index, cross_encoder_score) tuples,
            sorted by score descending, limited to top_k.
        """
        if not passages:
            return []
        self._load_model_if_needed()
        pairs = [[query, p] for p in passages]
        scores = self._model.predict(pairs)
        indexed_scores = list(
            enumerate(scores.tolist() if hasattr(scores, "tolist") else list(scores))
        )
        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        return indexed_scores[:top_k]


_reranker_instance: Reranker | None = None
_reranker_attempted: bool = False


def get_reranker() -> Reranker | None:
    """Get or create the singleton reranker instance.

    Unlike @lru_cache, this does not permanently cache None on transient
    failures. A process restart will retry model loading.
    """
    global _reranker_instance, _reranker_attempted
    if _reranker_instance is not None:
        return _reranker_instance
    if _reranker_attempted:
        return None
    _reranker_attempted = True
    try:
        reranker = Reranker()
        reranker._load_model_if_needed()
        _reranker_instance = reranker
        return reranker
    except Exception:
        logger.warning("Reranker unavailable; using retrieval scores only", exc_info=True)
        return None
