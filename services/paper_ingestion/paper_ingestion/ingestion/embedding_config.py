"""Embedding configuration constants and pure config/payload helpers.

Extracted verbatim from ``embedder.py`` (C1 God-class decomposition).
No behavior change — these are byte-for-byte the same constants and free
functions, relocated for cohesion.  ``embedder.py`` re-exports every public
name here so the import surface is unchanged.
"""

from __future__ import annotations

import re
import uuid

from paper_ingestion.config import get_paper_ingestion_settings

_cfg = get_paper_ingestion_settings()
EMBEDDING_MODEL = _cfg.embedding_model
EMBEDDING_MODEL_NAME = _cfg.embedding_model_name
EMBEDDING_DIMENSION = _cfg.embedding_dimension
EMBED_REQUEST_TIMEOUT_SECONDS = _cfg.embed_request_timeout_seconds
QDRANT_URL = _cfg.qdrant_url

COLLECTION_NAME = "paper_chunks"
CHUNK_TOKEN_LIMIT = 512
CHUNK_OVERLAP_TOKENS = 50

# Stable namespace for deterministic Qdrant point IDs derived from (paper_id, chunk_index).
# Using uuid5(namespace, "paper_id:chunk_index") guarantees the same point ID across retries
# so Qdrant upsert is idempotent and a failed Phase-3 (DB write) cannot accumulate duplicates.
# uuid.NAMESPACE_DNS is the standard RFC 4122 public DNS namespace — a stable, well-known value.
_CHUNK_POINT_ID_NAMESPACE = uuid.NAMESPACE_DNS

_KNOWN_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "nomic-embed-text": 768,
    "qwen3-embedding:0.6b": 1024,
    "qwen3-embedding:4b": 2560,
}

_SENSITIVE_ERROR_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"(sk-[A-Za-z0-9._-]+)|"
    r"(Authorization:\s*)[^\s,;]+",
    re.IGNORECASE,
)


def _sanitize_embedding_error_detail(text: str, *, max_chars: int = 200) -> str:
    """Return a compact provider-error preview without secrets or noisy whitespace."""
    compact = " ".join(text.split())
    redacted = _SENSITIVE_ERROR_RE.sub(
        lambda match: f"{match.group(1) or match.group(3) or ''}<redacted>",
        compact,
    )
    return redacted[:max_chars]


def validate_embedding_configuration(
    *,
    model_name: str | None = None,
    dimension: int | None = None,
) -> None:
    """Fail clearly when a known fixed-dimension embedding model is misconfigured."""
    model_name = model_name or EMBEDDING_MODEL_NAME
    dimension = EMBEDDING_DIMENSION if dimension is None else dimension
    normalized_model_name = model_name.lower()
    for known_model, expected_dimension in _KNOWN_EMBEDDING_DIMENSIONS.items():
        if known_model in normalized_model_name and dimension != expected_dimension:
            raise RuntimeError(
                f"Embedding configuration mismatch: {model_name} outputs "
                f"{expected_dimension} dimensions, but EMBEDDING_DIMENSION={dimension}. "
                "Update EMBEDDING_DIMENSION or finish the Qdrant re-embed checkpoint."
            )


def extract_qdrant_collection_dimension(collection_info: object) -> int | None:
    """Return the single-vector collection size from Qdrant collection metadata."""
    config = getattr(collection_info, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    if vectors is None and isinstance(params, dict):
        vectors = params.get("vectors")
    if isinstance(vectors, dict):
        vector_config = vectors.get("") or next(iter(vectors.values()), None)
        if isinstance(vector_config, dict):
            return vector_config.get("size")
        return getattr(vector_config, "size", None)
    return getattr(vectors, "size", None)


def raise_for_collection_dimension_mismatch(
    collection_name: str,
    current_dimension: int | None,
    *,
    expected_dimension: int | None = None,
    model_name: str | None = None,
) -> None:
    """Raise when an existing Qdrant collection does not match the active embed config."""
    expected_dimension = EMBEDDING_DIMENSION if expected_dimension is None else expected_dimension
    model_name = model_name or EMBEDDING_MODEL_NAME
    if current_dimension is None:
        return  # unknown dimension (no size in collection info) — nothing to compare against
    if current_dimension == expected_dimension:
        return
    raise RuntimeError(
        f"Qdrant collection {collection_name!r} has dimension {current_dimension}; "
        f"expected {expected_dimension} for {model_name}. "
        "Run the documented Qdrant checkpoint/re-embed flow before restarting."
    )


def _point_payload(hit) -> dict | None:
    """Return a Qdrant point payload when present, else ``None``."""
    payload = getattr(hit, "payload", None)
    return payload if isinstance(payload, dict) else None


def _user_scope_filter(
    user_id: int | None,
    library_paper_ids: list[int] | None = None,
):
    """Return a Qdrant Filter scoping search to the caller, or None when unscoped.

    The ``should`` branches are OR-combined.  Base scope: chunks the caller
    embedded (``user_id == X``) OR canonical chunks (``user_id`` payload IS NULL).

    ``library_paper_ids`` (PI-RAG-001) widens the scope so the caller can also
    retrieve chunks for ANY paper in **their own** library, regardless of which
    user originally embedded them.  This fixes secondary-library under-fetch on
    shared-corpus papers (e.g. paper P processed by user A, where A's chunks
    carry ``user_id == A``, but P is legitimately in caller B's library).

    Security: the widening is keyed strictly on the CALLER'S own
    ``user_library`` membership — the caller must supply only their own
    library's paper_ids.  A paper that is NOT in the caller's library (e.g. a
    private upload owned solely by another user) is never added to this branch
    and therefore stays out of the candidate set.  The defense-in-depth DB
    visibility check in ``rag/streaming.py`` remains the backstop.
    """
    if user_id is None:
        return None
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        IsNullCondition,
        MatchAny,
        MatchValue,
        PayloadField,
    )

    should: list = [
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        IsNullCondition(is_null=PayloadField(key="user_id")),
    ]
    if library_paper_ids:
        should.append(FieldCondition(key="paper_id", match=MatchAny(any=library_paper_ids)))
    return Filter(should=should)
