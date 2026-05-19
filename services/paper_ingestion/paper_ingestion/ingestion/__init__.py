"""Ingestion subpackage — embedding, retrieval, reranking, and recommendations.

Only the names that have live production callers are re-exported here
(audit A1-08, 2026-05-19).  Use explicit submodule imports for everything else:
    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.ingestion.reranker import get_reranker
"""

from paper_ingestion.ingestion.recommender import refresh_recommendations

__all__ = [
    "refresh_recommendations",
]
