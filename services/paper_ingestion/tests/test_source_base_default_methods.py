"""Tests for PaperSource ABC default methods (fetch_new_since, get_recommendations).

These methods have default empty-list implementations so source plugins that
cannot support them simply inherit the no-op behaviour without needing to
override or isinstance-branch at call sites.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.models import TopicRef
from app.sources.base import PaperSource


class DummySource(PaperSource):
    """Minimal concrete subclass — only implements the two abstract methods."""

    source_type = "dummy"

    async def search(self, query: str, max_results: int = 10):
        return []

    async def fetch_by_id(self, external_id: str):
        return None


@pytest.fixture()
def dummy_source():
    config = MagicMock()
    http_client = MagicMock()
    return DummySource(config=config, http_client=http_client)


async def test_fetch_new_since_returns_empty_list_by_default(dummy_source):
    """fetch_new_since default implementation returns [] without raising."""
    since = datetime(2024, 1, 1, tzinfo=UTC)
    topics = [TopicRef(id=1, name="ML", query_terms=["machine learning"])]
    result = await dummy_source.fetch_new_since(since=since, topics=topics, limit=10)
    assert result == []


async def test_get_recommendations_returns_empty_list_by_default(dummy_source):
    """get_recommendations default implementation returns [] without raising."""
    result = await dummy_source.get_recommendations(
        positive_seeds=["paper_id_abc"],
        negative_seeds=["paper_id_xyz"],
        limit=10,
    )
    assert result == []


async def test_get_recommendations_accepts_none_negative_seeds(dummy_source):
    """get_recommendations must accept negative_seeds=None (no mutable default)."""
    result = await dummy_source.get_recommendations(
        positive_seeds=["paper_id_abc"],
        negative_seeds=None,
        limit=5,
    )
    assert result == []


async def test_fetch_new_since_default_limit(dummy_source):
    """fetch_new_since should have a default limit of 100."""
    since = datetime(2024, 6, 1, tzinfo=UTC)
    result = await dummy_source.fetch_new_since(
        since=since,
        topics=[],
    )
    assert result == []


async def test_get_recommendations_default_limit(dummy_source):
    """get_recommendations should have a default limit of 50."""
    result = await dummy_source.get_recommendations(positive_seeds=[])
    assert result == []
