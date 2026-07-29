"""Tests for PaperSource.consolidate_topics and SourceQuery dataclass.

PR-A2: default consolidate_topics implementation and SourceQuery API.
DRY-S1: _insert_run_history hoisted to base.
F-10: apply_startup_grace() hoisted to PaperSource base.
BE-06: _retry_after_seconds cap + attempt-1 guard (N/A — no persistent writer).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter
from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.arxiv_source import ArxivSource
from paper_ingestion.sources.base import PaperSource, SourceQuery, _MAX_RETRY_AFTER_S
from paper_ingestion.sources.openalex_source import OpenAlexSource
from paper_ingestion.sources.pubmed_source import PubMedSource
from paper_ingestion.sources.semantic_scholar_source import SemanticScholarSource

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


# ---------------------------------------------------------------------------
# DRY-S1: _insert_run_history hoisted to PaperSource base
# ---------------------------------------------------------------------------


def _make_config(source_type: SourceType) -> PaperSourceConfig:
    return PaperSourceConfig(id=1, source_type=source_type, enabled=True, config={})


@pytest.mark.parametrize(
    "source_cls,source_type_enum,expected_source_type",
    [
        (ArxivSource, SourceType.ARXIV, "arxiv"),
        (OpenAlexSource, SourceType.OPENALEX, "openalex"),
        (SemanticScholarSource, SourceType.SEMANTIC_SCHOLAR, "semantic_scholar"),
        (PubMedSource, SourceType.PUBMED, "pubmed"),
    ],
)
async def test_insert_run_history_hoisted_to_base(
    source_cls, source_type_enum, expected_source_type
):
    """_insert_run_history on base inserts correct source_type for each subclass."""
    mock_pool, mock_conn = make_pool_and_conn()
    config = _make_config(source_type_enum)
    source = source_cls(config=config, http_client=MagicMock(), db_pool=mock_pool)

    assert source.source_type == expected_source_type

    await source._insert_run_history(
        started_at=time.monotonic(),
        status="ok",
        candidate_count=5,
        duration_ms=100,
        user_id=None,
    )

    mock_conn.execute.assert_called_once()
    sql, *args = mock_conn.execute.call_args.args
    assert "source_run_history" in sql
    assert expected_source_type in args


async def test_insert_run_history_noops_when_db_pool_is_none():
    """_insert_run_history returns immediately without error when db_pool is None."""
    config = _make_config(SourceType.ARXIV)
    source = ArxivSource(config=config, http_client=MagicMock(), db_pool=None)

    await source._insert_run_history(
        started_at=time.monotonic(),
        status="ok",
        candidate_count=0,
        duration_ms=50,
        user_id=None,
    )


# ---------------------------------------------------------------------------
# F-10: apply_startup_grace hoisted to PaperSource base
# ---------------------------------------------------------------------------


async def test_apply_startup_grace_exists_on_base():
    """PaperSource.apply_startup_grace is an async method callable on any subclass."""
    source = StubSource(config=MagicMock(), http_client=MagicMock())
    assert hasattr(source, "apply_startup_grace"), "PaperSource must expose apply_startup_grace()"
    import inspect

    assert inspect.iscoroutinefunction(source.apply_startup_grace), (
        "apply_startup_grace must be a coroutine function"
    )


async def test_apply_startup_grace_reads_config_grace_seconds():
    """apply_startup_grace reads startup_grace_seconds from config.pulse and calls _enforce_startup_grace."""
    pulse_cfg = MagicMock()
    pulse_cfg.startup_grace_seconds = 5.0
    config = MagicMock()
    config.pulse = pulse_cfg

    source = StubSource(config=config, http_client=MagicMock())

    with patch(
        "paper_ingestion.sources.base._enforce_startup_grace", new_callable=AsyncMock
    ) as mock_enforce:
        await source.apply_startup_grace()

    mock_enforce.assert_awaited_once_with(5.0)


async def test_apply_startup_grace_defaults_to_zero_when_pulse_absent():
    """apply_startup_grace defaults to 0.0 when config has no pulse attribute."""
    config = MagicMock(spec=[])  # no attributes — getattr returns default

    source = StubSource(config=config, http_client=MagicMock())

    with patch(
        "paper_ingestion.sources.base._enforce_startup_grace", new_callable=AsyncMock
    ) as mock_enforce:
        await source.apply_startup_grace()

    mock_enforce.assert_awaited_once_with(0.0)


async def test_apply_startup_grace_defaults_to_zero_when_grace_seconds_absent():
    """apply_startup_grace defaults to 0.0 when config.pulse has no startup_grace_seconds."""
    pulse_cfg = MagicMock(spec=[])  # pulse exists but has no startup_grace_seconds
    config = MagicMock()
    config.pulse = pulse_cfg

    source = StubSource(config=config, http_client=MagicMock())

    with patch(
        "paper_ingestion.sources.base._enforce_startup_grace", new_callable=AsyncMock
    ) as mock_enforce:
        await source.apply_startup_grace()

    mock_enforce.assert_awaited_once_with(0.0)


@pytest.mark.parametrize(
    "source_cls,source_type_enum",
    [
        (ArxivSource, SourceType.ARXIV),
        (OpenAlexSource, SourceType.OPENALEX),
        (PubMedSource, SourceType.PUBMED),
        (SemanticScholarSource, SourceType.SEMANTIC_SCHOLAR),
    ],
)
async def test_apply_startup_grace_available_on_all_source_subclasses(source_cls, source_type_enum):
    """apply_startup_grace is callable on all four concrete source subclasses."""
    config = _make_config(source_type_enum)
    source = source_cls(config=config, http_client=MagicMock(), db_pool=None)

    with patch(
        "paper_ingestion.sources.base._enforce_startup_grace", new_callable=AsyncMock
    ) as mock_enforce:
        await source.apply_startup_grace()

    # grace_seconds is 0.0 since PaperSourceConfig has no pulse attr
    mock_enforce.assert_awaited_once_with(0.0)


# ---------------------------------------------------------------------------
# BE-06: _retry_after_seconds cap
# Attempt-1 guard: N/A — base.py has no persistent rate-limiter writer.
# Only source_run_history (an audit log) uses db_pool; no retry-slot persistence
# exists in this file, so there is no concurrent-insert race to guard.
# ---------------------------------------------------------------------------


def _make_response_with_retry_after(value: str) -> httpx.Response:
    """Build a minimal httpx.Response carrying a Retry-After header."""
    return httpx.Response(
        status_code=429,
        headers={"Retry-After": value},
    )


def test_retry_after_seconds_caps_absurdly_large_header(stub_source):
    """A Retry-After header value far exceeding _MAX_RETRY_AFTER_S is capped.

    BE-06: Ensures that a malicious or misbehaving upstream cannot force the
    poller to wait billions of seconds by sending ``Retry-After: 99999999999``.
    The returned value must be <= _MAX_RETRY_AFTER_S (3600 s).
    """
    response = _make_response_with_retry_after("99999999999")
    result = stub_source._retry_after_seconds(response)
    assert result is not None
    assert result <= _MAX_RETRY_AFTER_S, (
        f"Expected capped value <= {_MAX_RETRY_AFTER_S}, got {result}"
    )


def test_retry_after_seconds_preserves_reasonable_value(stub_source):
    """A Retry-After value within the cap is returned unchanged.

    Values <= _MAX_RETRY_AFTER_S (3600 s) must not be clamped down.
    """
    response = _make_response_with_retry_after("60")
    result = stub_source._retry_after_seconds(response)
    assert result == 60


def test_retry_after_seconds_cap_equals_one_hour():
    """_MAX_RETRY_AFTER_S module constant equals 3600 (one hour)."""
    assert _MAX_RETRY_AFTER_S == 3600


# ---------------------------------------------------------------------------
# B2-DRY-01: make_persistent_rate_limiter hoisted to PaperSource base
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_cls,source_type_enum,expected_source_type",
    [
        (ArxivSource, SourceType.ARXIV, "arxiv"),
        (OpenAlexSource, SourceType.OPENALEX, "openalex"),
        (SemanticScholarSource, SourceType.SEMANTIC_SCHOLAR, "semantic_scholar"),
        (PubMedSource, SourceType.PUBMED, "pubmed"),
    ],
)
def test_make_persistent_rate_limiter_wires_source_specifics(
    source_cls, source_type_enum, expected_source_type
):
    """The base helper builds a limiter keyed to the subclass source_type.

    Identical to the previous inline construction: source_type comes from the
    subclass, the in-memory limiter is passed as fallback, and the supplied
    min_interval_seconds is preserved verbatim.
    """
    mock_pool = MagicMock()
    config = _make_config(source_type_enum)
    source = source_cls(config=config, http_client=MagicMock(), db_pool=mock_pool)

    limiter = source.make_persistent_rate_limiter(user_id=7, min_interval_seconds=2.5)

    assert isinstance(limiter, PersistentSourceRateLimiter)
    assert limiter._source_type == expected_source_type
    assert limiter._user_id == 7
    assert limiter._min_interval == 2.5
    assert limiter._pool is mock_pool
    assert limiter._fallback is source._rate_limiter


def test_make_persistent_rate_limiter_returns_none_without_pool(stub_source):
    """No db_pool -> None (call sites fall back to the in-memory limiter)."""
    assert stub_source.db_pool is None
    assert stub_source.make_persistent_rate_limiter(user_id=1, min_interval_seconds=1.0) is None


# ---------------------------------------------------------------------------
# B2-DRY-01: _normalize_since_utc hoisted to PaperSource base
# ---------------------------------------------------------------------------


def test_normalize_since_utc_treats_naive_as_utc(stub_source):
    """A naive datetime is stamped with UTC without shifting its wall-clock value."""
    naive = datetime(2024, 3, 1, 12, 30)
    result = stub_source._normalize_since_utc(naive)
    assert result.tzinfo is UTC
    assert result == datetime(2024, 3, 1, 12, 30, tzinfo=UTC)


def test_normalize_since_utc_converts_aware_offset(stub_source):
    """An aware datetime in another offset is converted to the equivalent UTC instant."""
    plus_two = datetime(2024, 3, 1, 12, 30, tzinfo=timezone(timedelta(hours=2)))
    result = stub_source._normalize_since_utc(plus_two)
    assert result == datetime(2024, 3, 1, 10, 30, tzinfo=UTC)


def test_normalize_since_utc_is_idempotent_on_utc(stub_source):
    """An already-UTC datetime is returned unchanged."""
    aware = datetime(2024, 3, 1, 12, 30, tzinfo=UTC)
    assert stub_source._normalize_since_utc(aware) == aware
