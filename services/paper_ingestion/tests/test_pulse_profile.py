"""Tests for app.pulse.profile — UserProfile model and load_profile().

TDD: tests written before implementation.
All DB interaction is mocked via the asyncpg pool helpers from conftest.
"""

from unittest.mock import AsyncMock

import pytest
from paper_ingestion.models import TopicRef

# Path setup handled by conftest.py (already loaded by pytest).
# Import the module under test AFTER conftest stubs are installed.
from paper_ingestion.pulse.profile import UserProfile, load_profile
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
        "pulse.stage2_top_k": 40,
    }
    cfg = {**defaults, **(extra or {})}
    return [FakeRecord({"key": k, "value": v}) for k, v in cfg.items()]


def _make_paper_rows(count: int = 3) -> list[FakeRecord]:
    """Paper rows with id and abstract for centroid calculation."""
    return [FakeRecord({"id": i, "abstract": f"abstract text {i}"}) for i in range(1, count + 1)]


def _make_positive_rating_rows() -> list[FakeRecord]:
    return [
        FakeRecord({"id": i, "title": f"Positive Paper {i}"})
        for i in range(1, 12)  # 11 rows, top 10
    ]


def _make_negative_rating_rows() -> list[FakeRecord]:
    return [
        FakeRecord({"title": f"Negative Paper {i}"})
        for i in range(1, 5)  # 4 rows
    ]


def _make_neg_topic_rows() -> list[FakeRecord]:
    return [
        FakeRecord({"name": "Reinforcement Learning", "neg_count": 3}),
    ]


def _make_neg_author_rows() -> list[FakeRecord]:
    return [
        FakeRecord({"author": "Boring Author", "neg_count": 2}),
    ]


def _make_neg_abstract_rows(count: int = 2) -> list[FakeRecord]:
    return [
        FakeRecord({"id": i, "abstract": f"negative abstract {i}", "weight": 1.0})
        for i in range(1, count + 1)
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

    vec = fake_embedding_vector()
    neg_abstract_rows = _make_neg_abstract_rows(2)

    # conn.fetch returns different things on sequential calls (10 total)
    conn.fetch.side_effect = [
        topic_rows,  # 1. topics query
        author_rows,  # 2. tracked_authors query
        paper_rows,  # 3. engaged papers query
        config_rows,  # 4. user_config query
        positive_rows,  # 5. positive ratings query
        negative_rows,  # 6. negative ratings query
        _make_neg_topic_rows(),  # 7. L1 negative topics
        _make_neg_author_rows(),  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        neg_abstract_rows,  # 10. L2 negative abstracts
    ]

    # embed_texts: called once for library centroid (3 papers), once for negative centroid (2 papers)
    mock_embedder = AsyncMock()
    neg_vec = fake_embedding_vector()
    mock_embedder.embed_texts.side_effect = [
        [vec, vec, vec],  # library centroid call
        [neg_vec, neg_vec],  # negative centroid call
    ]

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
    assert len(profile.library_centroid) == len(vec)
    # Config
    assert profile.deck_size == 10
    assert profile.stage2_top_k == 40
    assert "embedding" in profile.weights
    # Rating history (top 10)
    assert len(profile.recent_positive_titles) == 10
    assert len(profile.recent_negative_titles) == 4
    # New Phase-A fields
    assert profile.negative_topics == ["Reinforcement Learning"]
    assert profile.negative_authors == ["Boring Author"]
    assert profile.negative_centroid is not None
    assert len(profile.negative_centroid) == len(neg_vec)
    assert profile.dampened_topics == set()
    # liked_paper_ids: all ids from positive_rows (not capped like recent_positive_titles)
    # _make_positive_rating_rows() returns 11 rows, all with ids → 11 liked_paper_ids
    assert len(profile.liked_paper_ids) == 11


# ---------------------------------------------------------------------------
# Empty library → centroid None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_empty_library_centroid_none():
    """When no engaged papers exist, library_centroid is None."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [],  # 1. topics
        [],  # 2. tracked_authors
        [],  # 3. engaged papers (empty)
        _make_config_rows(),  # 4. user_config
        [],  # 5. positive ratings
        [],  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts (empty → no neg centroid)
    ]

    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.library_centroid is None
    assert profile.topics == []
    assert profile.tracked_author_names == set()
    assert profile.tracked_author_s2_ids == set()
    assert profile.recent_positive_titles == []
    assert profile.recent_negative_titles == []
    # embed_texts should NOT be called (no papers to embed, no negative abstracts)
    mock_embedder.embed_texts.assert_not_called()
    # New Phase-A fields — all empty/None
    assert profile.negative_topics == []
    assert profile.negative_authors == []
    assert profile.negative_centroid is None
    assert profile.dampened_topics == set()


# ---------------------------------------------------------------------------
# Missing user_config keys → sensible defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_missing_config_uses_defaults():
    """When user_config has no pulse keys, defaults are applied."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [],  # 1. topics
        [],  # 2. authors
        [],  # 3. papers
        [],  # 4. config (empty!)
        [],  # 5. positives
        [],  # 6. negatives
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts
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
    # 15 positive ratings (include id field for liked_paper_ids extraction)
    pos_rows = [FakeRecord({"id": i, "title": f"Pos {i}"}) for i in range(15)]
    neg_rows = [FakeRecord({"title": f"Neg {i}"}) for i in range(15)]

    conn.fetch.side_effect = [
        [],  # 1. topics
        [],  # 2. tracked_authors
        [],  # 3. engaged papers
        _make_config_rows(),  # 4. user_config
        pos_rows,  # 5. positive ratings
        neg_rows,  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts
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

    # Single conn reused for both phases (fetch side_effect spans all 10 calls).
    conn = AsyncMock()
    conn.fetch.side_effect = [
        _make_topic_rows(),  # Phase 1: topics (fetch 1)
        _make_author_rows(),  # Phase 1: tracked_authors (fetch 2)
        _make_paper_rows(2),  # Phase 1: engaged papers (fetch 3)
        _make_config_rows(),  # Phase 3: user_config (fetch 4)
        [],  # Phase 3: positive ratings (fetch 5)
        [],  # Phase 3: negative ratings (fetch 6)
        [],  # Phase 3: L1 negative topics (fetch 7)
        [],  # Phase 3: L1 negative authors (fetch 8)
        [],  # Phase 3: L3 dampened topics (fetch 9)
        [],  # Phase 3: L2 negative abstracts (fetch 10)
    ]

    vec = fake_embedding_vector(4)

    # embed_texts records its position in the event list when called.
    # It is called twice: once for library centroid (Phase 2a), once for
    # negative centroid (Phase 2b) — but only if there are negative abstracts.
    # With empty neg abstracts, it's called only once.
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
        [],  # 1. topics
        [],  # 2. authors
        [],  # 3. engaged papers
        bad_config,  # 4. user_config with bad weights
        [],  # 5. positives
        [],  # 6. negatives
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts
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
        [],  # 1. no topics
        [],  # 2. no authors
        [],  # 3. no engaged papers
        _make_config_rows(),  # 4. valid config
        [],  # 5. no positive ratings
        [],  # 6. no negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts
    ]
    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.topics == []
    assert profile.deck_size > 0
    assert profile.stage2_top_k > 0
    # An empty-topics profile is valid — scoring stages must handle it
    assert isinstance(profile.weights, dict)


# ---------------------------------------------------------------------------
# PI-CORE-010: embeddings with mismatched dimensions are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_skips_embeddings_with_wrong_dim():
    """Embeddings whose dimension differs from embeddings[0] are skipped (PI-CORE-010).

    The centroid must be computed only from valid-dim embeddings.  If ALL
    embeddings are bad (impossible here since embeddings[0] is always valid),
    centroid becomes None.  Here we test the mixed case: 2 good + 1 bad → centroid
    from 2 vectors; and that the bad one does NOT corrupt the centroid dimensions.
    """
    from unittest.mock import AsyncMock

    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [],  # 1. topics
        [],  # 2. tracked_authors
        _make_paper_rows(3),  # 3. 3 engaged papers → embedder called with 3 abstracts
        _make_config_rows(),  # 4. user_config
        [],  # 5. positive ratings
        [],  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts (empty → no neg centroid embed)
    ]

    good_vec = fake_embedding_vector(4)  # dim 4 — the "expected" dimension
    bad_vec = fake_embedding_vector(8)  # dim 8 — mismatched

    mock_embedder = AsyncMock()
    # Return two good vectors and one bad vector (bad is in the middle).
    # Only one embed_texts call (library centroid); no neg centroid since no neg abstracts.
    mock_embedder.embed_texts.return_value = [good_vec, bad_vec, good_vec]

    profile = await load_profile(pool, embedder=mock_embedder)

    # Centroid should be computed from the two good vectors only
    assert profile.library_centroid is not None
    assert len(profile.library_centroid) == 4  # matches good_vec dimension

    # Centroid value: mean of good_vec[i] + good_vec[i] = good_vec[i] (same vec twice)
    for expected, actual in zip(good_vec, profile.library_centroid):
        assert abs(actual - expected) < 1e-9, (
            f"centroid[i]={actual} != good_vec[i]={expected}; bad embedding corrupted result"
        )


@pytest.mark.asyncio
async def test_load_profile_all_embeddings_wrong_dim_gives_none_centroid():
    """When all embeddings after the first have wrong dim, centroid uses only the first.

    embeddings[0] always defines expected_dim — it is always valid.  If there are
    no other valid vectors, centroid = embeddings[0] itself (n=1).  This test
    verifies the boundary where n=1 still produces a centroid (not None).
    """
    from unittest.mock import AsyncMock

    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [],  # 1. topics
        [],  # 2. tracked_authors
        _make_paper_rows(3),  # 3. engaged papers
        _make_config_rows(),  # 4. user_config
        [],  # 5. positive ratings
        [],  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts (empty → no neg centroid embed)
    ]

    good_vec = fake_embedding_vector(4)
    bad_vec = fake_embedding_vector(16)  # mismatched

    mock_embedder = AsyncMock()
    # First is good, remaining two are bad.
    # Only one embed_texts call (library centroid); no neg centroid since no neg abstracts.
    mock_embedder.embed_texts.return_value = [good_vec, bad_vec, bad_vec]

    profile = await load_profile(pool, embedder=mock_embedder)

    # n=1 — centroid is just good_vec / 1 = good_vec
    assert profile.library_centroid is not None
    assert len(profile.library_centroid) == 4
    for expected, actual in zip(good_vec, profile.library_centroid):
        assert abs(actual - expected) < 1e-9


# ---------------------------------------------------------------------------
# Phase-A new paths: dampening cap + negative centroid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_profile_dampening_cap_truncation(caplog):
    """L3 dampened_topics is capped at floor(0.5 × topic_count) when over-represented.

    With 2 topics and 3 dampened topic rows, the cap is floor(0.5 × 2) = 1.
    Only the top 1 row (highest neg_count) should survive; a warning must be logged.
    """
    import logging

    pool, conn = _make_pool_and_conn()

    # 2 topics → cap = floor(0.5 × 2) = 1
    topic_rows = _make_topic_rows()  # ids 1, 2
    # 3 dampened rows — more than the cap of 1
    dampened_rows = [
        FakeRecord({"id": 10, "neg_count": 8}),  # highest — survives cap
        FakeRecord({"id": 11, "neg_count": 6}),  # truncated
        FakeRecord({"id": 12, "neg_count": 5}),  # truncated
    ]

    conn.fetch.side_effect = [
        topic_rows,  # 1. topics (2 rows)
        [],  # 2. tracked_authors
        [],  # 3. engaged papers
        _make_config_rows(),  # 4. user_config
        [],  # 5. positive ratings
        [],  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        dampened_rows,  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts
    ]
    mock_embedder = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.profile"):
        profile = await load_profile(pool, embedder=mock_embedder)

    # Cap applied: only topic id 10 (highest neg_count) survives
    assert profile.dampened_topics == {10}
    # Warning must be emitted about truncation
    assert any("dampened_topics" in record.message for record in caplog.records), (
        f"Expected dampening cap warning, got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_load_profile_negative_centroid_computed_when_neg_abstracts_present():
    """L2 negative centroid is computed when negative abstracts exist.

    With 2 negative abstract rows, embed_texts should be called twice total:
    once for library centroid (empty → skipped) and once for the negative centroid.
    """
    pool, conn = _make_pool_and_conn()
    neg_abstract_rows = _make_neg_abstract_rows(2)  # 2 rows, weight=1.0 each

    conn.fetch.side_effect = [
        [],  # 1. topics
        [],  # 2. tracked_authors
        [],  # 3. engaged papers (empty → no library centroid embed call)
        _make_config_rows(),  # 4. user_config
        [],  # 5. positive ratings
        [],  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        neg_abstract_rows,  # 10. L2 negative abstracts (2 rows)
    ]

    vec = fake_embedding_vector(8)
    mock_embedder = AsyncMock()
    # Only called once: for negative centroid (library centroid is skipped — no engaged papers).
    mock_embedder.embed_texts.return_value = [vec, vec]

    profile = await load_profile(pool, embedder=mock_embedder)

    # Library centroid is None (no engaged papers)
    assert profile.library_centroid is None
    # Negative centroid should be computed from the two weighted abstract embeddings
    assert profile.negative_centroid is not None
    assert len(profile.negative_centroid) == 8
    # Both weights are 1.0 and both vecs are identical → centroid == vec
    for expected, actual in zip(vec, profile.negative_centroid):
        assert abs(actual - expected) < 1e-9, f"negative_centroid[i]={actual} != vec[i]={expected}"
    # embed_texts called exactly once (no library centroid call when no abstracts)
    mock_embedder.embed_texts.assert_called_once()


@pytest.mark.asyncio
async def test_load_profile_negative_centroid_none_when_no_neg_abstracts():
    """L2 negative_centroid is None when there are no negative abstracts."""
    pool, conn = _make_pool_and_conn()

    conn.fetch.side_effect = [
        [],  # 1. topics
        [],  # 2. tracked_authors
        [],  # 3. engaged papers
        _make_config_rows(),  # 4. user_config
        [],  # 5. positive ratings
        [],  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts (empty)
    ]
    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.negative_centroid is None
    # embed_texts never called (no library abstracts and no negative abstracts)
    mock_embedder.embed_texts.assert_not_called()
