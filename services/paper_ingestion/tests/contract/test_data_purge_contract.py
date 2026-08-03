"""Real-PostgreSQL proof that a purge leaves vectors and rows agreeing.

The retention set is computed by production SQL over real rows (public scope OR
membership in a surviving user's library). Only that query can decide which of a
departed user's points are redacted and which are removed, so this is the one
place the whole split is exercised end to end: an in-memory point store applies
the filters the job builds, and the assertions read the resulting collection.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest

from jarvis_common.testing import SharedConnPool
from paper_ingestion.jobs import data_purge

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


class _PointStore:
    """Minimal Qdrant stand-in that evaluates the job's own filters over points."""

    def __init__(self, points: list[dict[str, Any]]) -> None:
        self.points = points

    @staticmethod
    def _condition_holds(condition: Any, point: dict[str, Any]) -> bool:
        value = point.get(condition.key)
        match = condition.match
        if hasattr(match, "any"):
            return value in match.any
        return value == match.value

    def _selected(self, selector: Any) -> list[dict[str, Any]]:
        return [
            point
            for point in self.points
            if all(self._condition_holds(c, point) for c in (selector.must or []))
            and not any(self._condition_holds(c, point) for c in (selector.must_not or []))
        ]

    async def count(self, collection_name: str, count_filter: Any, exact: bool) -> Any:
        return SimpleNamespace(count=len(self._selected(count_filter)))

    async def set_payload(
        self, collection_name: str, payload: dict[str, Any], points: Any, wait: bool
    ) -> None:
        for point in self._selected(points):
            point.update(payload)

    async def delete(self, collection_name: str, points_selector: Any, wait: bool) -> None:
        removed = {id(point) for point in self._selected(points_selector)}
        self.points = [point for point in self.points if id(point) not in removed]


# Verified: services/paper_ingestion/paper_ingestion/jobs/data_purge.py:104
# Verified: services/paper_ingestion/paper_ingestion/jobs/data_purge.py:209
async def test_purge_redacts_retained_vectors_and_deletes_unreferenced_ones(
    contract_conn: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public and survivor-held papers keep redacted vectors; a private one loses its own."""
    expired_id = await contract_conn.fetchval(
        "INSERT INTO users (email, deleted_at)"
        " VALUES ('purge-split-expired@contract.example.com', NOW() - INTERVAL '60 days')"
        " RETURNING id"
    )
    survivor_id = await contract_conn.fetchval(
        "INSERT INTO users (email) VALUES ('purge-split-survivor@contract.example.com')"
        " RETURNING id"
    )
    papers = await contract_conn.fetch(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url,
               discovered_by, visibility_scope
           ) VALUES
               ('purge-split-public', 'arxiv', 'Public retained', ARRAY['A'],
                'https://example.test/purge-split-public', $1, 'public'),
               ('purge-split-library', 'local', 'Library retained', ARRAY['A'],
                'https://example.test/purge-split-library', $1, 'private'),
               ('purge-split-private', 'local', 'Private only', ARRAY['A'],
                'https://example.test/purge-split-private', $1, 'private')
           RETURNING external_id, id""",
        expired_id,
    )
    paper_ids = {row["external_id"]: row["id"] for row in papers}
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        survivor_id,
        paper_ids["purge-split-library"],
    )

    store = _PointStore(
        [
            {"paper_id": paper_ids["purge-split-public"], "user_id": expired_id},
            {"paper_id": paper_ids["purge-split-library"], "user_id": expired_id},
            {"paper_id": paper_ids["purge-split-private"], "user_id": expired_id},
            {"paper_id": paper_ids["purge-split-public"], "user_id": survivor_id},
        ]
    )
    monkeypatch.setattr(data_purge, "_anonymize_audit_log_for_users", AsyncMock(return_value=0))
    monkeypatch.setattr(data_purge, "log_audit", AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(db_pool=SharedConnPool(contract_conn), qdrant_client=store)
    )

    await data_purge.data_purge_task(app)

    surviving = {(point["paper_id"], point["user_id"]) for point in store.points}
    assert (paper_ids["purge-split-public"], None) in surviving, "public paper lost its vector"
    assert (paper_ids["purge-split-library"], None) in surviving, (
        "a paper still in a surviving user's library lost its vector"
    )
    assert paper_ids["purge-split-private"] not in {p["paper_id"] for p in store.points}, (
        "an unreferenced private paper's vector outlived its owner"
    )
    assert (paper_ids["purge-split-public"], survivor_id) in surviving, (
        "another user's attribution was redacted"
    )
