"""Focused test doubles and value factories for paper sources."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.arxiv_source import ArxivSource
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.openalex_source import OpenAlexSource
from paper_ingestion.sources.semantic_scholar_source import SemanticScholarSource


def make_source[SourceT: PaperSource](
    source_type: SourceType,
    source_class: type[SourceT],
    http_client: httpx.AsyncClient | None = None,
    *,
    source_id: int,
    api_key: str | None = None,
    db_pool: Any = None,
) -> SourceT:
    """Build a source with explicit identity, configuration, and collaborators."""
    source_config = PaperSourceConfig(
        id=source_id,
        source_type=source_type,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    return source_class(
        source_config,
        http_client or httpx.AsyncClient(),
        db_pool=db_pool,
    )


def make_arxiv_source(
    *,
    http_client: httpx.AsyncClient | None = None,
    db_pool: Any = None,
) -> ArxivSource:
    """Build an arXiv source with its canonical test identity."""
    return make_source(
        SourceType.ARXIV,
        ArxivSource,
        http_client,
        source_id=1,
        db_pool=db_pool,
    )


def make_openalex_source(
    api_key: str | None = "test-oa-key",
    *,
    http_client: httpx.AsyncClient | None = None,
    db_pool: Any = None,
) -> OpenAlexSource:
    """Build an OpenAlex source with its canonical test identity."""
    return make_source(
        SourceType.OPENALEX,
        OpenAlexSource,
        http_client,
        source_id=3,
        api_key=api_key,
        db_pool=db_pool,
    )


def make_semantic_scholar_source(
    api_key: str | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    source_id: int = 2,
    db_pool: Any = None,
) -> SemanticScholarSource:
    """Build a Semantic Scholar source with configurable test identity."""
    return make_source(
        SourceType.SEMANTIC_SCHOLAR,
        SemanticScholarSource,
        http_client,
        source_id=source_id,
        api_key=api_key,
        db_pool=db_pool,
    )


def make_topic(
    name: str,
    query_terms: list[str] | None = None,
    topic_id: int = 1,
) -> TopicRef:
    """Build a topic reference suitable for source polling."""
    return TopicRef(
        id=topic_id,
        name=name,
        query_terms=query_terms or [name],
    )


def mock_log_event_pool() -> MagicMock:
    """Return a mock asyncpg pool that records ``execute`` calls.

    Suitable for tests that exercise ``log_event`` emission when a real pool
    is supplied to a source (``db_pool=pool``).
    """
    return make_pool_and_conn(with_transaction=False)[0]
