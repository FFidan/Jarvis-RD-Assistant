"""Tests for app.pulse.profile — UserProfile model and load_profile().

TDD: tests written before implementation.
All DB interaction is mocked via the asyncpg pool helpers from conftest.
"""

from unittest.mock import AsyncMock

import pytest
from app.models import TopicRef

# Path setup handled by conftest.py (already loaded by pytest).
# Import the module under test AFTER conftest stubs are installed.
from app.pulse.profile import UserProfile, load_profile

from tests.conftest import FakeRecord, _make_pool_and_conn, fake_embedding_vector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_topic_rows() -> list[FakeRecord]:
    return [
        FakeRecord(
            {
                "id": 1,
                "name": "Neural ODEs",
                "description": "Continuous-depth neural networks",
                "query_terms": ["neural ODE", "continuous dynamics"],
            }
        ),
        FakeRecord(
            {
                "id": 2,
                "name": "Transformers",
                "description": None,
                "query_terms": ["attention mechanism", "BERT"],
            }
        ),
    ]


def _make_author_rows() -> list[FakeRecord]:
    return [
        FakeRecord({"s2_author_id": "A1234", "author_name": "Jane Doe"}),
        FakeRecord({"s2_author_id": None, "author_name": "John Smith"}),
    ]


def _make_config_rows(extra: dict | None = None) -> list[FakeRecord]:
    defaults = {
        "pulse.weights": {
            "embedding": 0.2,
            "topic": 0.2,
            "llm_relevance": 0.3,
            "llm_novelty": 0.1,
            "author_bonus": 0.15,
            "recency": 0.05,
        },
        "pulse.deck_size": 10,
        "pulse.stage2_top_k": 50,
    }
    cfg = {**defaults, **(extra or {})}
    return [FakeRecord({"key": k, "value": v}) for k, v in cfg.items()]


def _make_paper_rows(count: int = 3) -> list[FakeRecord]:
    """Paper rows with id and abstract for centroid calculation."""
    return [FakeRecord({"id": i, "abstract": f"abstract text {i}"}) for i in range(1, count + 1)]


def _make_positive_rating_rows() -> list[FakeRecord]:
    return [
        FakeRecord({"title": f"Positive Paper {i}"})
        for i in range(1, 12)  # 11 rows, top 10
    ]


def _make_negative_rating_rows() -> list[FakeRecord]:
    return [
        FakeRecord({"title": f"Negative Paper {i}"})
        for i in range(1, 5)  # 4 rows
    ]


# ---------------------------------------------------------------------------
# Happy path: all data present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_happy_path():
    """load_profile returns a fully populated UserProfile when all data present."""
    pool, conn = _make_pool_and_conn()
    topic_rows = _make_topic_rows()
    author_rows = _make_author_rows()
    config_rows = _make_config_rows()
    paper_rows = _make_paper_rows(3)
    positive_rows = _make_positive_rating_rows()
    negative_rows = _make_negative_rating_rows()

    vec = fake_embedding_vector(768)

    # conn.fetch returns different things on sequential calls
    conn.fetch.side_effect = [
        topic_rows,  # topics query
        author_rows,  # tracked_authors query
        paper_rows,  # engaged papers query
        config_rows,  # user_config query
        positive_rows,  # positive ratings query
        negative_rows,  # negative ratings query
    ]

    # embed_texts: called once per paper abstract (3 papers)
    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.return_value = [vec, vec, vec]

    profile = await load_profile(pool, embedder=mock_embedder)

    assert isinstance(profile, UserProfile)
    # Topics
    assert len(profile.topics) == 2
    assert profile.topics[0].name == "Neural ODEs"
    assert profile.topics[1].description is None
    # Tracked author S2 IDs (only non-None s2_author_ids)
    assert "A1234" in profile.tracked_author_s2_ids
    # centroid should be computed
    assert profile.library_centroid is not None
    assert len(profile.library_centroid) == 768
    # Config
    assert profile.deck_size == 10
    assert profile.stage2_top_k == 50
    assert "embedding" in profile.weights
    # Rating history (top 10)
    assert len(profile.recent_positive_titles) == 10
    assert len(profile.recent_negative_titles) == 4


# ---------------------------------------------------------------------------
# Empty library → centroid None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_empty_library_centroid_none():
    """When no engaged papers exist, library_centroid is None."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [],  # topics
        [],  # tracked_authors
        [],  # engaged papers (empty)
        _make_config_rows(),  # user_config
        [],  # positive ratings
        [],  # negative ratings
    ]

    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.library_centroid is None
    assert profile.topics == []
    assert profile.tracked_author_names == set()
    assert profile.tracked_author_s2_ids == set()
    assert profile.recent_positive_titles == []
    assert profile.recent_negative_titles == []
    # embed_texts should NOT be called (no papers to embed)
    mock_embedder.embed_texts.assert_not_called()


# ---------------------------------------------------------------------------
# Missing user_config keys → sensible defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_missing_config_uses_defaults():
    """When user_config has no pulse keys, defaults are applied."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [],  # topics
        [],  # authors
        [],  # papers
        [],  # config (empty!)
        [],  # positives
        [],  # negatives
    ]
    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    # Defaults must be positive integers
    assert profile.deck_size > 0
    assert profile.stage2_top_k > 0
    # weights dict must be non-empty with at least embedding key
    assert isinstance(profile.weights, dict)
    assert len(profile.weights) > 0


# ---------------------------------------------------------------------------
# Rating history ordering: top 10 most recent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_rating_history_top10():
    """Positive titles are capped at 10, negative at 10."""
    pool, conn = _make_pool_and_conn()
    # 15 positive ratings
    pos_rows = [FakeRecord({"title": f"Pos {i}"}) for i in range(15)]
    neg_rows = [FakeRecord({"title": f"Neg {i}"}) for i in range(15)]

    conn.fetch.side_effect = [
        [],
        [],
        [],
        _make_config_rows(),
        pos_rows,
        neg_rows,
    ]
    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    assert len(profile.recent_positive_titles) == 10
    assert len(profile.recent_negative_titles) == 10


# ---------------------------------------------------------------------------
# UserProfile model validation
# ---------------------------------------------------------------------------


def test_user_profile_model_fields():
    """UserProfile Pydantic model accepts expected field types."""
    profile = UserProfile(
        topics=[TopicRef(id=1, name="Test", description=None, query_terms=[])],
        tracked_author_names={"alice smith"},
        tracked_author_s2_ids={"A123"},
        library_centroid=[0.1, 0.2, 0.3],
        weights={"embedding": 0.5, "topic": 0.5},
        deck_size=10,
        stage2_top_k=50,
        recent_positive_titles=["Title A"],
        recent_negative_titles=["Title B"],
    )
    assert profile.deck_size == 10
    assert profile.library_centroid is not None
    assert len(profile.library_centroid) == 3


def test_user_profile_centroid_none_allowed():
    """UserProfile allows library_centroid=None."""
    profile = UserProfile(
        topics=[],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={},
        deck_size=5,
        stage2_top_k=20,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )
    assert profile.library_centroid is None
