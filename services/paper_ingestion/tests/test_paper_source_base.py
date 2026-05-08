"""Tests for PaperSource.consolidate_topics and SourceQuery dataclass.

PR-A2: default consolidate_topics implementation and SourceQuery API.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from paper_ingestion.models import TopicRef
from paper_ingestion.sources.base import PaperSource, SourceQuery

# ---------------------------------------------------------------------------
# Minimal concrete subclass
# ---------------------------------------------------------------------------


class StubSource(PaperSource):
    """Minimal concrete PaperSource — only satisfies abstract method contract."""

    source_type = "stub"

    async def search(self, query: str, max_results: int = 10, **kwargs):  # type: ignore[override]
        return []

    async def fetch_by_id(self, external_id: str):
        return None


@pytest.fixture()
def stub_source() -> StubSource:
    return StubSource(config=MagicMock(), http_client=MagicMock())


# ---------------------------------------------------------------------------
# SourceQuery dataclass tests
# ---------------------------------------------------------------------------


def test_source_query_is_frozen():
    """SourceQuery is a frozen dataclass — attributes are immutable."""
    q = SourceQuery(topics=[], extra_params={})
    with pytest.raises(Exception):  # FrozenInstanceError
        q.topics = []  # type: ignore[misc]


def test_source_query_default_extra_params():
    """extra_params defaults to an empty dict."""
    t = TopicRef(id=1, name="ML")
    q = SourceQuery(topics=[t])
    assert q.extra_params == {}


def test_source_query_equality():
    """Two SourceQuery instances with same topics/params are equal."""
    t = TopicRef(id=1, name="AI")
    q1 = SourceQuery(topics=[t], extra_params={"sort": "date"})
    q2 = SourceQuery(topics=[t], extra_params={"sort": "date"})
    assert q1 == q2


# ---------------------------------------------------------------------------
# consolidate_topics default behaviour tests
# ---------------------------------------------------------------------------


def test_default_consolidate_topics_returns_one_per_topic(stub_source):
    """Default implementation returns exactly one SourceQuery per topic."""
    topics = [
        TopicRef(id=1, name="ML"),
        TopicRef(id=2, name="NLP"),
        TopicRef(id=3, name="CV"),
    ]
    queries = stub_source.consolidate_topics(topics)
    assert len(queries) == 3
    for i, q in enumerate(queries):
        assert isinstance(q, SourceQuery)
        assert q.topics == [topics[i]]


def test_default_consolidate_topics_empty_list(stub_source):
    """Empty topic list produces empty query list."""
    queries = stub_source.consolidate_topics([])
    assert queries == []


def test_default_consolidate_topics_single_topic(stub_source):
    """Single topic produces one SourceQuery wrapping that topic."""
    t = TopicRef(id=42, name="Robotics", query_terms=["robots"])
    queries = stub_source.consolidate_topics([t])
    assert len(queries) == 1
    assert queries[0].topics == [t]
    assert queries[0].extra_params == {}


def test_default_consolidate_topics_is_deterministic(stub_source):
    """Same topics input → identical query output on repeated calls."""
    topics = [TopicRef(id=i, name=f"Topic{i}") for i in range(5)]
    first = stub_source.consolidate_topics(topics)
    second = stub_source.consolidate_topics(topics)
    assert first == second


def test_consolidate_topics_each_query_has_exactly_one_topic(stub_source):
    """Default: each returned SourceQuery contains exactly one topic."""
    topics = [TopicRef(id=i, name=f"T{i}") for i in range(4)]
    queries = stub_source.consolidate_topics(topics)
    for q in queries:
        assert len(q.topics) == 1
