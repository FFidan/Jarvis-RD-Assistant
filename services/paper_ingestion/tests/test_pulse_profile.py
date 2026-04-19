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


# ---------------------------------------------------------------------------
# BE-001: connection released before embed_texts is called (PI-006)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conn_released_before_embed():
    """embed_texts must be called AFTER the first connection is released.

    This verifies the BE-001 / PI-006 refactor: the HTTP call to the embedder
    must not hold a database connection (Phase 1 conn released → Phase 2 embed
    → Phase 3 conn re-acquired).
    """
    from unittest.mock import AsyncMock, MagicMock

    events: list[str] = []

    # Build an acquire context manager that records enter/exit per call.
    def _make_acquire_ctx(label: str) -> MagicMock:
        ctx = MagicMock()

        async def _aenter(*_a, **_kw):
            events.append(f"acquire:{label}")
            return conn

        async def _aexit(*_a, **_kw):
            events.append(f"release:{label}")
            return False

        ctx.__aenter__ = _aenter
        ctx.__aexit__ = _aexit
        return ctx

    # Pool hands out a *new* ctx object for each acquire() call.
    pool = MagicMock()
    pool.acquire.side_effect = [_make_acquire_ctx("c1"), _make_acquire_ctx("c2")]

    # Single conn reused for both phases (fetch side_effect spans all 6 calls).
    conn = AsyncMock()
    conn.fetch.side_effect = [
        _make_topic_rows(),  # Phase 1: topics
        _make_author_rows(),  # Phase 1: tracked_authors
        _make_paper_rows(2),  # Phase 1: engaged papers
        _make_config_rows(),  # Phase 3: user_config
        [],  # Phase 3: positive ratings
        [],  # Phase 3: negative ratings
    ]

    vec = fake_embedding_vector(4)

    # embed_texts records its position in the event list when called.
    async def _embed(texts):
        events.append("embed_texts")
        return [vec] * len(texts)

    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.side_effect = _embed

    profile = await load_profile(pool, embedder=mock_embedder)

    # Basic sanity: profile is correct
    assert isinstance(profile, UserProfile)
    assert profile.library_centroid is not None

    # Ordering assertion: "release:c1" must appear before "embed_texts"
    assert "embed_texts" in events, "embed_texts was never called"
    release_idx = events.index("release:c1")
    embed_idx = events.index("embed_texts")
    assert release_idx < embed_idx, (
        f"embed_texts called at position {embed_idx} but first connection was only "
        f"released at position {release_idx}; events={events}"
    )

    # And the second connection must be acquired AFTER embed_texts
    acquire2_idx = events.index("acquire:c2")
    assert embed_idx < acquire2_idx, (
        f"Second connection acquired at {acquire2_idx} before embed at {embed_idx}; events={events}"
    )


# ---------------------------------------------------------------------------
# Config validator edge cases: bad weights / bad deck_size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_bad_weights_value_falls_back_to_defaults():
    """When pulse.weights is not a dict (e.g. a bare string), defaults are used."""
    from unittest.mock import AsyncMock

    pool, conn = _make_pool_and_conn()
    # Simulate a corrupted / wrong-typed config value
    bad_config = [
        FakeRecord({"key": "pulse.weights", "value": "not-a-dict"}),
        FakeRecord({"key": "pulse.deck_size", "value": 10}),
        FakeRecord({"key": "pulse.stage2_top_k", "value": 50}),
    ]
    conn.fetch.side_effect = [
        [],  # topics
        [],  # authors
        [],  # engaged papers
        bad_config,  # user_config with bad weights
        [],  # positives
        [],  # negatives
    ]
    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    # Weights must fall back to the defaults (non-empty dict with known keys)
    assert isinstance(profile.weights, dict)
    assert len(profile.weights) > 0
    assert "embedding" in profile.weights


@pytest.mark.asyncio
async def test_load_profile_empty_topics_produces_valid_profile():
    """load_profile with no topics still produces a fully usable UserProfile."""
    from unittest.mock import AsyncMock

    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [],  # no topics
        [],  # no authors
        [],  # no engaged papers
        _make_config_rows(),  # valid config
        [],  # no positive ratings
        [],  # no negative ratings
    ]
    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.topics == []
    assert profile.deck_size > 0
    assert profile.stage2_top_k > 0
    # An empty-topics profile is valid — scoring stages must handle it
    assert isinstance(profile.weights, dict)
