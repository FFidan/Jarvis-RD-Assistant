"""In-process Qdrant sidecar for boundary-adapter tests.

The class implements the async subset used by JARVIS' embedding/search code:
collection lifecycle, upsert, query, scroll, count, payload update, and delete. It preserves
Qdrant's payload-filter semantics closely enough for user-scope and paper-scope
contracts without requiring a Qdrant container.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class FauxQdrantPoint:
    """Stored point returned by the faux Qdrant client."""

    id: Any
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class _Collection:
    dimension: int
    points: dict[Any, FauxQdrantPoint] = field(default_factory=dict)
    payload_indexes: dict[str, Any] = field(default_factory=dict)


class FauxQdrantClient:
    """Small async stand-in for ``qdrant_client.AsyncQdrantClient``."""

    def __init__(self) -> None:
        """Initialise with an empty in-memory collection store."""
        self._collections: dict[str, _Collection] = {}

    async def close(self) -> None:
        """No-op; the in-memory store is simply garbage-collected."""
        return None

    async def collection_exists(self, collection_name: str) -> bool:
        """Return ``True`` if a collection with this name has been created."""
        return collection_name in self._collections

    async def get_collections(self) -> SimpleNamespace:
        """Return a namespace whose ``.collections`` lists all created collection names."""
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in sorted(self._collections)]
        )

    async def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:
        """Create a named collection with the vector dimension extracted from *vectors_config*."""
        size = _vector_size(vectors_config)
        if size is None:
            raise ValueError("vectors_config must include a size")
        self._collections[collection_name] = _Collection(dimension=int(size))

    async def get_collection(self, *, collection_name: str) -> SimpleNamespace:
        """Return a namespace mirroring the Qdrant ``CollectionInfo`` shape."""
        collection = self._require_collection(collection_name)
        vectors = SimpleNamespace(size=collection.dimension)
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)),
            payload_schema=dict(collection.payload_indexes),
        )

    async def create_payload_index(
        self,
        *,
        collection_name: str,
        field_name: str,
        field_schema: Any,
        **_: Any,
    ) -> SimpleNamespace:
        """Record an idempotent payload index on an existing collection."""
        collection = self._require_collection(collection_name)
        collection.payload_indexes[field_name] = field_schema
        return SimpleNamespace(status="completed", operation_id=1)

    async def upsert(self, *, collection_name: str, points: list[Any], **_: Any) -> SimpleNamespace:
        """Insert or overwrite *points* in the named collection; validates vector dimension."""
        collection = self._require_collection(collection_name)
        for point in points:
            vector = [float(v) for v in getattr(point, "vector")]
            if len(vector) != collection.dimension:
                raise ValueError(
                    f"vector dimension {len(vector)} != collection dimension {collection.dimension}"
                )
            collection.points[getattr(point, "id")] = FauxQdrantPoint(
                id=getattr(point, "id"),
                vector=vector,
                payload=dict(getattr(point, "payload", {}) or {}),
            )
        return SimpleNamespace(status="completed", operation_id=1)

    async def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int = 10,
        query_filter: Any = None,
        score_threshold: float | None = None,
        with_payload: bool = True,
        **_: Any,
    ) -> SimpleNamespace:
        """Score all matching points by cosine similarity and return the top *limit*."""
        collection = self._require_collection(collection_name)
        scored: list[FauxQdrantPoint] = []
        for point in collection.points.values():
            if not _matches_filter(point.payload, query_filter):
                continue
            score = _cosine([float(v) for v in query], point.vector)
            if score_threshold is not None and score < score_threshold:
                continue
            scored.append(
                FauxQdrantPoint(
                    id=point.id,
                    vector=point.vector,
                    payload=point.payload if with_payload else {},
                    score=score,
                )
            )
        scored.sort(key=lambda p: p.score, reverse=True)
        return SimpleNamespace(points=scored[:limit])

    async def scroll(
        self,
        *,
        collection_name: str,
        scroll_filter: Any = None,
        limit: int = 10,
        with_payload: bool = True,
        with_vectors: bool = False,
        **_: Any,
    ) -> tuple[list[FauxQdrantPoint], None]:
        """Return up to *limit* filtered points; always returns ``None`` as the next-page token."""
        collection = self._require_collection(collection_name)
        points: list[FauxQdrantPoint] = []
        for point in collection.points.values():
            if not _matches_filter(point.payload, scroll_filter):
                continue
            points.append(
                FauxQdrantPoint(
                    id=point.id,
                    vector=point.vector if with_vectors else [],
                    payload=point.payload if with_payload else {},
                )
            )
        return points[:limit], None

    async def count(
        self,
        *,
        collection_name: str,
        count_filter: Any = None,
        exact: bool = True,  # noqa: ARG002
        **_: Any,
    ) -> SimpleNamespace:
        """Count points in the collection that match *count_filter*."""
        collection = self._require_collection(collection_name)
        total = sum(
            1
            for point in collection.points.values()
            if _matches_filter(point.payload, count_filter)
        )
        return SimpleNamespace(count=total)

    async def delete(
        self,
        *,
        collection_name: str,
        points_selector: Any = None,
        **_: Any,
    ) -> SimpleNamespace:
        """Delete points by ID list or filter from the named collection."""
        collection = self._require_collection(collection_name)
        ids = _selector_ids(points_selector)
        if ids is not None:
            for point_id in ids:
                collection.points.pop(point_id, None)
        else:
            selector_filter = _selector_filter(points_selector)
            for point_id, point in list(collection.points.items()):
                if _matches_filter(point.payload, selector_filter):
                    collection.points.pop(point_id, None)
        return SimpleNamespace(status="completed", operation_id=1)

    async def set_payload(
        self,
        *,
        collection_name: str,
        payload: dict[str, Any],
        points: Any,
        **_: Any,
    ) -> SimpleNamespace:
        """Merge *payload* into points selected by IDs or a Qdrant filter."""
        collection = self._require_collection(collection_name)
        ids = _selector_ids(points)
        if ids is not None:
            selected = (
                collection.points[point_id]
                for point_id in ids
                if point_id in collection.points
            )
        else:
            selector_filter = _selector_filter(points)
            selected = (
                point
                for point in collection.points.values()
                if _matches_filter(point.payload, selector_filter)
            )
        for point in selected:
            point.payload.update(payload)
        return SimpleNamespace(status="completed", operation_id=1)

    def _require_collection(self, collection_name: str) -> _Collection:
        try:
            return self._collections[collection_name]
        except KeyError as exc:
            raise KeyError(f"collection {collection_name!r} does not exist") from exc


def _vector_size(vectors_config: Any) -> int | None:
    if isinstance(vectors_config, dict):
        vector_config = vectors_config.get("") or next(iter(vectors_config.values()), None)
        if isinstance(vector_config, dict):
            return vector_config.get("size")
        return getattr(vector_config, "size", None)
    return getattr(vectors_config, "size", None)


def _cosine(query: list[float], vector: list[float]) -> float:
    if not query or not vector:
        return 0.0
    if len(query) != len(vector):
        raise ValueError(f"dimension mismatch: {len(query)} vs {len(vector)}")
    n = min(len(query), len(vector))
    dot = sum(query[i] * vector[i] for i in range(n))
    q_norm = math.sqrt(sum(v * v for v in query[:n]))
    v_norm = math.sqrt(sum(v * v for v in vector[:n]))
    if q_norm == 0 or v_norm == 0:
        return 0.0
    return dot / (q_norm * v_norm)


def _matches_filter(payload: dict[str, Any], qdrant_filter: Any) -> bool:
    if qdrant_filter is None:
        return True
    must = getattr(qdrant_filter, "must", None)
    must_not = getattr(qdrant_filter, "must_not", None)
    should = getattr(qdrant_filter, "should", None)
    if must and not all(_matches_condition(payload, condition) for condition in must):
        return False
    if must_not and any(_matches_condition(payload, condition) for condition in must_not):
        return False
    if should and not any(_matches_condition(payload, condition) for condition in should):
        return False
    return True


def _matches_condition(payload: dict[str, Any], condition: Any) -> bool:
    field = getattr(condition, "key", None)
    match = getattr(condition, "match", None)
    if field is not None and match is not None:
        # MatchAny(any=[...]) — payload value must be one of the listed values.
        any_values = getattr(match, "any", None)
        if any_values is not None:
            return payload.get(field) in any_values
        # MatchValue(value=...) — exact match.
        return payload.get(field) == getattr(match, "value", None)

    is_null = getattr(condition, "is_null", None)
    if is_null is not None:
        null_field = getattr(is_null, "key", None)
        if null_field is None:
            return False
        return payload.get(null_field) is None

    nested_filter = getattr(condition, "filter", None)
    if nested_filter is not None:
        return _matches_filter(payload, nested_filter)

    # A bare ``Filter`` nested as a condition (Qdrant's Condition union
    # includes Filter — used to AND a user-scope sub-filter into an outer
    # ``must`` list, e.g. search_chunks_in_paper / discover_from_seeds).
    # Recurse with full must/should/must_not semantics so nested scope
    # filters stay restrictive, exactly as in real Qdrant.
    if (
        getattr(condition, "must", None) is not None
        or getattr(condition, "should", None) is not None
        or getattr(condition, "must_not", None) is not None
    ):
        return _matches_filter(payload, condition)

    return True


def _selector_ids(points_selector: Any) -> list[Any] | None:
    if points_selector is None:
        return None
    points = getattr(points_selector, "points", None)
    if points is not None:
        return list(points)
    if isinstance(points_selector, list):
        return points_selector
    return None


def _selector_filter(points_selector: Any) -> Any:
    if points_selector is None:
        return None
    return getattr(points_selector, "filter", points_selector)
