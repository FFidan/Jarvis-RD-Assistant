"""Back-compat shim — re-exports everything from paper_ingestion.ingestion.reranker.

Existing imports like ``from paper_ingestion.reranker import get_reranker`` continue
to work unchanged.  New code should import from the canonical location:
    from paper_ingestion.ingestion.reranker import get_reranker
"""

from paper_ingestion.ingestion.reranker import (
    Reranker,
    _reranker_state,
    _RerankerState,
    get_reranker,
)

__all__ = [
    "Reranker",
    "_reranker_state",
    "_RerankerState",
    "get_reranker",
]
