"""In-process Qdrant sidecar for boundary-adapter tests.

The class implements the async subset used by JARVIS' embedding/search code:
collection lifecycle, upsert, query, scroll, count, and delete.  It preserves
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


class FauxQdrantClient:
    """Small async stand-in for ``qdrant_client.AsyncQdrantClient``."""

    def __init__(self) -> None:
        self._collections: dict[str, _Collection] = {}

    async def close(self) -> None:
        return None

    async def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self._collections

    async def get_collections(self) -> SimpleNamespace:
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in sorted(self._collections)]
        )

    async def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:
        size = _vector_size(vectors_config)
        if size is None:
            raise ValueError("vectors_config must include a size")
        self._collections[collection_name] = _Collection(dimension=int(size))

    async def get_collection(self, *, collection_name: str) -> SimpleNamespace:
        collection = self._require_collection(collection_name)
        vectors = SimpleNamespace(size=collection.dimension)
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))

    async def upsert(self, *, collection_name: str, points: list[Any], **_: Any) -> SimpleNamespace:
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
        return payload.get(field) == getattr(match, "value", None)

    is_null = getattr(condition, "is_null", None)
    if is_null is not None:
        null_field = getattr(is_null, "key", None)
        return payload.get(null_field) is None

    nested_filter = getattr(condition, "filter", None)
    if nested_filter is not None:
        return _matches_filter(payload, nested_filter)

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
