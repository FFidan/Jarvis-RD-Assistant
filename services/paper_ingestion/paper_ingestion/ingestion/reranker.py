"""Cross-encoder reranker for improving retrieval quality.

Re-scores query-passage pairs using a cross-encoder model to produce
more accurate relevance rankings than bi-encoder similarity alone.

The reranker is optional. Set ``RERANKER_ENABLED=true`` in the environment
and ensure ``sentence-transformers`` (from ``requirements-optional.txt``) is
installed to activate it. When disabled or unavailable, :func:`get_reranker`
returns ``None`` and callers fall back to retrieval-score ordering.
"""

import logging
from typing import Any

try:
    from sentence_transformers import CrossEncoder

    _HAS_RERANKER = True
except ImportError:
    CrossEncoder = None  # type: ignore[assignment,misc]
    _HAS_RERANKER = False

logger = logging.getLogger(__name__)


class Reranker:
    """Cross-encoder reranker using sentence-transformers.

    Parameters
    ----------
    model_name : str
        HuggingFace model name for the cross-encoder.
    """

    def __init__(self, model_name: str = "mixedbread-ai/mxbai-rerank-base-v2"):
        self._model_name = model_name
        self._model: Any | None = None
        self._load_failed: bool = False

    def _load_model_if_needed(self) -> None:
        """Lazy-load the cross-encoder model on first use (PyTorch backend).

        Tries CUDA first and falls back to CPU. Latches ``_load_failed`` so a
        failed load is not retried within the process.
        """
        if self._model is not None:
            return
        if self._load_failed:
            raise RuntimeError("reranker model load previously failed; not retrying within process")
        if CrossEncoder is None:  # sentence-transformers not installed
            raise RuntimeError("sentence-transformers is not installed; cannot load reranker")

        cross_encoder_cls = CrossEncoder  # narrowed: not None past the guard above

        try:
            self._model = cross_encoder_cls(self._model_name, device="cuda")
            logger.info("Cross-encoder loaded on PyTorch/CUDA: %s", self._model_name)
        except Exception:
            logger.warning("CUDA unavailable for cross-encoder, falling back to CPU")
            try:
                self._model = cross_encoder_cls(self._model_name, device="cpu")
                logger.info("Cross-encoder loaded on PyTorch/CPU: %s", self._model_name)
            except (OSError, ImportError, RuntimeError, FileNotFoundError):
                self._load_failed = True
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
        if self._model is None:
            raise RuntimeError("reranker model should be loaded")
        pairs = [[query, p] for p in passages]
        scores = self._model.predict(pairs)
        raw = scores.tolist() if hasattr(scores, "tolist") else list(scores)
        indexed_scores: list[tuple[int, float]] = [(i, float(s)) for i, s in enumerate(raw)]
        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        return indexed_scores[:top_k]


class _RerankerState:
    """Module-level state holder for the singleton reranker.

    Using a class instance avoids ``global`` while preserving the
    "attempt once per process" semantic that ``@lru_cache`` cannot provide
    (lru_cache permanently caches ``None`` on transient load failures).
    """

    def __init__(self) -> None:
        self.instance: Reranker | None = None
        self.attempted: bool = False

    def get(self) -> Reranker | None:
        """Return the reranker, loading it on the first call."""
        if self.instance is not None:
            return self.instance
        if self.attempted:
            return None
        self.attempted = True
        try:
            from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

            model = get_paper_ingestion_settings().reranker_model
            reranker = Reranker(model_name=model)
            reranker._load_model_if_needed()
            self.instance = reranker
            return reranker
        except Exception:
            logger.warning("Reranker unavailable; using retrieval scores only", exc_info=True)
            return None

    def reset(self) -> None:
        """Clear the singleton so the next get() re-initialises (useful in tests/eval)."""
        self.instance = None
        self.attempted = False


_reranker_state = _RerankerState()

# Latches once so an enabled-but-unavailable reranker is reported a single time
# rather than on every query.
_WARNED_MISSING_DEPENDENCY = False


def get_reranker() -> Reranker | None:
    """Get or create the singleton reranker instance.

    Returns ``None`` when the reranker is disabled (``RERANKER_ENABLED`` env
    var is not ``true``/``1``/``yes``) or when ``sentence-transformers`` is
    not installed.

    Unlike @lru_cache, this does not permanently cache None on transient
    failures. A process restart will retry model loading.
    """
    from jarvis_common.settings import get_reranker_settings

    if not get_reranker_settings().reranker_enabled:
        return None
    if not _HAS_RERANKER:
        # Asked for, but the dependency is not in this image — the CPU image omits it
        # on purpose. Say so once instead of silently degrading: the caller would
        # otherwise see plain retrieval scores and no reason why.
        global _WARNED_MISSING_DEPENDENCY
        if not _WARNED_MISSING_DEPENDENCY:
            _WARNED_MISSING_DEPENDENCY = True
            logger.warning(
                "RERANKER_ENABLED is set but sentence-transformers is not installed in "
                "this image; using retrieval scores only. The published CUDA image "
                "includes it; on a CPU install set INSTALL_OPTIONAL=true in .env and "
                "re-run ./setup.sh --build-local."
            )
        return None
    return _reranker_state.get()
