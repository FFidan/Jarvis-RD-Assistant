"""Ingestion subpackage — embedding, retrieval, reranking, and recommendations.

Re-exports the primary public surface so callers can use either:
    from paper_ingestion.ingestion import Embedder
or the canonical long-form:
    from paper_ingestion.ingestion.embedder import Embedder
"""

from paper_ingestion.ingestion.embedder import (
    CHUNK_OVERLAP_TOKENS,
    CHUNK_TOKEN_LIMIT,
    COLLECTION_NAME,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_NAME,
    QDRANT_URL,
    Embedder,
)
from paper_ingestion.ingestion.recommender import refresh_recommendations
from paper_ingestion.ingestion.reranker import Reranker, get_reranker

__all__ = [
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_TOKEN_LIMIT",
    "COLLECTION_NAME",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "EMBEDDING_MODEL_NAME",
    "QDRANT_URL",
    "Embedder",
    "Reranker",
    "get_reranker",
    "refresh_recommendations",
]
