"""Cross-encoder reranker for improving retrieval quality.

Re-scores query-passage pairs using a cross-encoder model to produce
more accurate relevance rankings than bi-encoder similarity alone.
"""

import logging
from functools import lru_cache

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

    def _load_model_if_needed(self):
        """Lazy-load the cross-encoder model on first use."""
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self._model_name)
                logger.info("Loaded cross-encoder model: %s", self._model_name)
            except ImportError:
                logger.warning("sentence-transformers not installed; reranking disabled")
                raise
            except Exception:
                logger.exception("Failed to load cross-encoder model")
                raise

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


@lru_cache(maxsize=1)
def get_reranker() -> Reranker | None:
    """Get or create the singleton reranker instance.

    Returns the cached :class:`Reranker` on success, or ``None`` when the
    cross-encoder model cannot be loaded (e.g. ``sentence-transformers`` is
    not installed, the model download fails, or any other initialisation
    error occurs).  Callers **must** handle ``None`` by falling back to
    raw retrieval scores.

    Because the result is ``@lru_cache``-d, the first call determines the
    outcome for the lifetime of the process.
    """
    try:
        reranker = Reranker()
        reranker._load_model_if_needed()
        return reranker
    except Exception:
        logger.warning(
            "Reranker unavailable; using retrieval scores only", exc_info=True
        )
        return None
