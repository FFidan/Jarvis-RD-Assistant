"""Back-compat shim — re-exports everything from paper_ingestion.ingestion.embedder.

Existing imports like ``from paper_ingestion.embedder import Embedder`` continue
to work unchanged.  New code should import from the canonical location:
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
    _point_payload,
    extract_qdrant_collection_dimension,
    raise_for_collection_dimension_mismatch,
    validate_embedding_configuration,
)

__all__ = [
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_TOKEN_LIMIT",
    "COLLECTION_NAME",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "EMBEDDING_MODEL_NAME",
    "QDRANT_URL",
    "Embedder",
    "extract_qdrant_collection_dimension",
    "_point_payload",
    "raise_for_collection_dimension_mismatch",
    "validate_embedding_configuration",
]
