"""Research Qdrant erasure primitive retained for Platform coordination."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QdrantPurgeCounts:
    """Counts of Qdrant points deleted or retained with redacted audit data.

    ``residual_points`` is how many points still carried the purged identifier
    after both writes completed; anything above zero means the vector store and
    the relational store disagree. It defaults so that callers constructing the
    pre-existing two-field shape keep working.
    """

    deleted: int
    redacted: int
    residual_points: int = 0


async def _purge_qdrant_for_user(
    qdrant: Any,
    uid: int,
    protected_paper_ids: list[int],
) -> QdrantPurgeCounts:
    """Erase a user's audit identifier from Qdrant before hard deletion.

    Parameters
    ----------
    qdrant : Any
        Async Qdrant client.
    uid : int
        User identifier being erased.
    protected_paper_ids : list[int]
        Papers that must retain their vectors because they are persisted public
        or belong to a surviving user's library.

    Returns
    -------
    QdrantPurgeCounts
        Exact pre-operation counts for deleted and redacted points.

    Notes
    -----
    Protected points are redacted before unprotected points are deleted. If
    either write fails, the exception propagates and the caller defers the SQL
    hard delete. ``set_payload`` changes only payload fields and preserves each
    point's vector.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        MatchAny,
        MatchValue,
    )

    from paper_ingestion.ingestion.embedder import COLLECTION_NAME  # noqa: PLC0415

    uid_condition = FieldCondition(key="user_id", match=MatchValue(value=uid))
    protected_condition = (
        FieldCondition(key="paper_id", match=MatchAny(any=protected_paper_ids))
        if protected_paper_ids
        else None
    )
    # Migration 0104: paper_chunks are canonical paper data, not user-owned
    # records. A point is deleted only when its paper is neither public nor in a
    # surviving user's library; every other point of the purged user is redacted,
    # never deleted.
    delete_filter = Filter(
        must=[uid_condition],
        must_not=[protected_condition] if protected_condition is not None else None,
    )
    delete_count_result = await qdrant.count(
        collection_name=COLLECTION_NAME,
        count_filter=delete_filter,
        exact=True,
    )
    deleted = int(delete_count_result.count)

    redacted = 0
    if protected_condition is not None:
        redact_filter = Filter(must=[uid_condition, protected_condition])
        redact_count_result = await qdrant.count(
            collection_name=COLLECTION_NAME,
            count_filter=redact_filter,
            exact=True,
        )
        redacted = int(redact_count_result.count)
        await qdrant.set_payload(
            collection_name=COLLECTION_NAME,
            payload={"user_id": None},
            points=redact_filter,
            wait=True,
        )

    # A Qdrant filter carrying no conditions matches EVERY point, so a selector
    # that lost its user scope would erase the whole shared collection.
    if not delete_filter.must:
        raise ValueError(
            "refusing a match-all Qdrant delete: the purge selector lost its user condition"
        )
    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=delete_filter,
        wait=True,
    )

    # Both writes have landed, so no point should still carry the purged
    # identifier: redacted points now hold ``user_id: None`` and the rest are
    # gone. A non-zero count is the two stores disagreeing.
    residual_result = await qdrant.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(must=[uid_condition]),
        exact=True,
    )
    residual_points = int(residual_result.count)
    if residual_points:
        logger.warning(
            "data_purge: %d Qdrant point(s) still carry user %d after the purge",
            residual_points,
            uid,
        )
    return QdrantPurgeCounts(deleted=deleted, redacted=redacted, residual_points=residual_points)


async def data_purge_task(app: Any) -> None:
    """Compatibility no-op; Platform's erasure coordinator owns deletion."""
    _ = app
    logger.info("data_purge is retired; Platform erasure coordination is authoritative")


def register_data_purge(scheduler: Any, app: Any) -> None:
    """Retain the call site without registering a second erasure scheduler."""
    _ = scheduler, app
    logger.info("data_purge scheduler registration is retired")
