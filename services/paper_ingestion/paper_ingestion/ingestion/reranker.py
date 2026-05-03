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

    def _load_model_if_needed(self) -> None:
        """Lazy-load the cross-encoder model on first use.

        Uses the ONNX backend (via ``optimum[onnxruntime]``) when available,
        which gives ~30% faster CPU inference and removes the torch runtime
        dependency after the one-time export+cache step.  Falls back to plain
        PyTorch if ``optimum`` is not installed or if ONNX export fails.
        """
        if self._model is not None:
            return
        if CrossEncoder is None:  # sentence-transformers not installed
            raise RuntimeError("sentence-transformers is not installed; cannot load reranker")

        cross_encoder_cls = CrossEncoder  # narrowed: not None past the guard above

        _onnx_kwargs: dict[str, Any] = {
            "backend": "onnx",
            "model_kwargs": {"provider": "CPUExecutionProvider"},
        }

        def _load(device: str, use_onnx: bool) -> Any:
            kwargs: dict[str, Any] = (
                {**_onnx_kwargs, "device": device} if use_onnx else {"device": device}
            )
            return cross_encoder_cls(self._model_name, **kwargs)

        # Determine whether optimum+onnxruntime are present.
        try:
            import onnxruntime  # noqa: F401
            import optimum  # noqa: F401

            _onnx_available = True
        except ImportError:
            _onnx_available = False

        try:
            self._model = _load("cuda", _onnx_available)
            backend_tag = "ONNX/CUDA" if _onnx_available else "PyTorch/CUDA"
            logger.info("Cross-encoder loaded on %s: %s", backend_tag, self._model_name)
        except Exception:
            logger.warning("CUDA unavailable for cross-encoder, falling back to CPU")
            try:
                self._model = _load("cpu", _onnx_available)
                backend_tag = "ONNX/CPU" if _onnx_available else "PyTorch/CPU"
                logger.info("Cross-encoder loaded on %s: %s", backend_tag, self._model_name)
            except Exception:
                if _onnx_available:
                    logger.warning(
                        "ONNX backend failed; retrying with PyTorch CPU for cross-encoder"
                    )
                    self._model = cross_encoder_cls(self._model_name, device="cpu")
                    logger.info("Cross-encoder loaded on PyTorch/CPU: %s", self._model_name)
                else:
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
            reranker = Reranker()
            reranker._load_model_if_needed()
            self.instance = reranker
            return reranker
        except Exception:
            logger.warning("Reranker unavailable; using retrieval scores only", exc_info=True)
            return None


_reranker_state = _RerankerState()


def get_reranker() -> Reranker | None:
    """Get or create the singleton reranker instance.

    Returns ``None`` when the reranker is disabled (``RERANKER_ENABLED`` env
    var is not ``true``/``1``/``yes``) or when ``sentence-transformers`` is
    not installed.

    Unlike @lru_cache, this does not permanently cache None on transient
    failures. A process restart will retry model loading.
    """
    from jarvis_common.settings import get_reranker_settings

    if not get_reranker_settings().reranker_enabled or not _HAS_RERANKER:
        return None
    return _reranker_state.get()
