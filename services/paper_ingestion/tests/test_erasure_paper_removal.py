"""The Research erasure phase removes sole-owned papers before the capability runs.

The set of papers to remove is derived from library membership, and
``research.erase_user_data`` deletes those rows. Running the two in the other
order leaves every assertion about erased data still true and removes nothing,
so the order is pinned here rather than left to the reading.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from paper_ingestion.routers import internal_domains


class _Request:
    """Minimal signed-subject stand-in for the Platform erasure principal."""

    def __init__(self, user_id: int) -> None:
        self.state = type("S", (), {"identity_principal": "platform", "user_id": user_id})()
        self.headers: dict[str, str] = {}


class _Pool:
    """Pool whose acquired connection records every capability call."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def acquire(self):  # noqa: ANN201 — async context manager stand-in
        calls = self._calls

        class _Ctx:
            async def __aenter__(self):  # noqa: ANN204
                connection = AsyncMock()
                connection.execute.side_effect = lambda sql, *a: calls.append(sql)
                return connection

            async def __aexit__(self, *exc: object) -> bool:
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_sole_owned_papers_are_removed_before_the_erasure_capability(monkeypatch):
    """The removal runs while the library rows it derives its set from still exist."""
    calls: list[str] = []

    async def _record(conn: object, user_id: int) -> list[int]:
        calls.append("erase_orphaned_user_papers")
        return []

    monkeypatch.setattr(internal_domains, "erase_orphaned_user_papers", _record)

    result = await internal_domains.erase_user_research_data(
        request_id=uuid.uuid4(),
        body=internal_domains.ErasureRequest(user_id=71),
        request=_Request(71),
        db_pool=_Pool(calls),
    )

    assert result["acknowledged"] is True
    assert calls[0] == "erase_orphaned_user_papers"
    assert "research.erase_user_data" in calls[1]


@pytest.mark.asyncio
async def test_a_caller_that_is_not_the_signed_subject_removes_nothing(monkeypatch):
    """A rejected erasure command deletes no paper, no chunk and no stored file."""
    calls: list[str] = []
    monkeypatch.setattr(
        internal_domains,
        "erase_orphaned_user_papers",
        AsyncMock(side_effect=AssertionError("must not run for a rejected caller")),
    )

    with pytest.raises(HTTPException) as raised:
        await internal_domains.erase_user_research_data(
            request_id=uuid.uuid4(),
            body=internal_domains.ErasureRequest(user_id=71),
            request=_Request(72),
            db_pool=_Pool(calls),
        )

    assert raised.value.status_code == 403
    assert calls == []
