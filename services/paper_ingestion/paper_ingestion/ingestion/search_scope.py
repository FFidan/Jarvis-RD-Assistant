"""Search visibility scope objects shared by retrieval callers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchScope:
    """User and explicit-paper restrictions for global chunk search."""

    user_id: int | None = None
    library_paper_ids: list[int] | None = None
    allowed_paper_ids: list[int] | None = None
