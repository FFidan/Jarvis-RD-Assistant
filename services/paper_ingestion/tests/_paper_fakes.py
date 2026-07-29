"""Typed value factories for paper-ingestion tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from paper_ingestion.models import PaperCreate, SourceType


def make_paper_create(
    external_id: str = "arxiv:0001",
    title: str = "Test Paper",
    source_type: SourceType | str = SourceType.ARXIV,
    *,
    authors: Sequence[str] | None = None,
    abstract: str | None = "Test abstract",
    published_date: date | None = None,
    url: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PaperCreate:
    """Build a paper with stable defaults and independent mutable fields."""
    return PaperCreate(
        external_id=external_id,
        source_type=SourceType(source_type),
        title=title,
        authors=list(authors) if authors is not None else ["Author A"],
        abstract=abstract,
        published_date=published_date,
        url=url if url is not None else f"https://example.test/{external_id}",
        metadata=dict(metadata) if metadata is not None else {},
    )
