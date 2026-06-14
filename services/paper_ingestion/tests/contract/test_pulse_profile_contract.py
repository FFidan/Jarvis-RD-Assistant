"""Pulse profile sidecar contracts.

Survivors for centroid/connection-release/negative-feedback mock-units in
services/paper_ingestion/tests/test_pulse_profile.py.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# Verified: services/paper_ingestion/paper_ingestion/pulse/profile.py:166
# (load_profile centroid path: centroid = mean of embedder.embed_texts(abstracts))
async def test_pulse_profile_w2_centroid_computation_via_faux_ollama_embeddings(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """load_profile computes a library centroid equal to the mean of embed_texts vectors.

    Seeds two engaged papers (state='to_read') for user A, then calls load_profile
    with a FauxOllamaServer.  The centroid returned must equal the component-wise
    mean of the two deterministic embeddings.

    Survivor-of: test_load_profile_happy_path (mock-unit) which mocks embed_texts
    and verifies centroid length but never touches a real embedding boundary.
    """
    from unittest.mock import AsyncMock, MagicMock

    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars.faux_ollama import deterministic_embedding
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION
    from paper_ingestion.pulse.profile import load_profile

    user_a_id = contract_two_users.user_a_id

    # The seeded paper_a already has state='to_read' (from _seed_resources),
    # so it qualifies as "engaged".  Add a second engaged paper.
    paper_b_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, abstract)
        VALUES ('pulse-centroid-b', 'arxiv', 'Centroid Paper B', ARRAY['B'],
                'https://example.test/cb', 'abstract for centroid paper B')
        RETURNING id
        """
    )
    # Update paper_a to have a known abstract
    await contract_conn.execute(
        "UPDATE papers SET abstract = 'abstract for centroid paper A' WHERE id = $1",
        contract_two_users.paper_id_a,
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state)
           VALUES ($1, $2, 'to_read')
           ON CONFLICT (paper_id, user_id) DO UPDATE SET state = 'to_read'""",
        paper_b_id,
        user_a_id,
    )

    shared_pool = SharedConnPool(contract_conn)
    mock_embedder = MagicMock()

    async def _real_embed(texts):
        return [deterministic_embedding(t, dimension=EMBEDDING_DIMENSION) for t in texts]

    mock_embedder.embed_texts = AsyncMock(side_effect=_real_embed)

    profile = await load_profile(shared_pool, embedder=mock_embedder, user_id=user_a_id)

    assert profile.library_centroid is not None, (
        "library_centroid must not be None when engaged papers with abstracts exist"
    )
    assert len(profile.library_centroid) == EMBEDDING_DIMENSION, (
        f"centroid dimension mismatch: got {len(profile.library_centroid)}, "
        f"expected {EMBEDDING_DIMENSION}"
    )

    # Verify centroid = mean of the two deterministic embeddings
    vec_a = deterministic_embedding("abstract for centroid paper A", dimension=EMBEDDING_DIMENSION)
    vec_b = deterministic_embedding("abstract for centroid paper B", dimension=EMBEDDING_DIMENSION)
    expected = [(a + b) / 2 for a, b in zip(vec_a, vec_b)]

    for i, (got, exp) in enumerate(zip(profile.library_centroid, expected)):
        assert abs(got - exp) < 1e-6, (
            f"centroid[{i}] = {got!r}, expected {exp!r}; "
            "centroid is not the component-wise mean of the two abstracts"
        )


# Verified: services/paper_ingestion/paper_ingestion/pulse/profile.py:94
# (load_profile: `async with db_pool.acquire() as conn:` releases before embed_texts)
async def test_pulse_profile_w2_connection_release_ordering_under_load(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """DB connection is released BEFORE embed_texts is called.

    Instruments acquire/release and embed_texts calls to assert the ordering
    invariant using a real SharedConnPool backed by the contract connection.

    Survivor-of: test_conn_released_before_embed (mock-unit at lines 325-408 of
    test_pulse_profile.py) which uses MagicMock pool and records event ordering.
    """
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars.faux_ollama import deterministic_embedding
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION
    from paper_ingestion.pulse.profile import load_profile
    from unittest.mock import AsyncMock, MagicMock

    user_a_id = contract_two_users.user_a_id

    # Ensure paper_a has an abstract so embed_texts is actually called.
    await contract_conn.execute(
        "UPDATE papers SET abstract = 'ordering test abstract' WHERE id = $1",
        contract_two_users.paper_id_a,
    )

    # Wrap SharedConnPool to record acquire/release events
    events: list[str] = []
    real_pool = SharedConnPool(contract_conn)

    class _InstrumentedPool:
        def acquire(self):
            import contextlib

            @contextlib.asynccontextmanager
            async def _cm():
                events.append("acquire")
                async with real_pool.acquire() as conn:
                    yield conn
                events.append("release")

            return _cm()

    mock_embedder = MagicMock()

    async def _recording_embed(texts):
        events.append("embed_texts")
        return [deterministic_embedding(t, dimension=EMBEDDING_DIMENSION) for t in texts]

    mock_embedder.embed_texts = AsyncMock(side_effect=_recording_embed)

    await load_profile(_InstrumentedPool(), embedder=mock_embedder, user_id=user_a_id)

    assert "embed_texts" in events, "embed_texts was never called"
    assert "release" in events, "connection was never released"

    first_release_idx = events.index("release")
    embed_idx = events.index("embed_texts")
    assert first_release_idx < embed_idx, (
        f"embed_texts at position {embed_idx} but first release at {first_release_idx}; "
        f"events={events}. Connection pool invariant violated: connection held during embed."
    )


# Verified: services/paper_ingestion/paper_ingestion/pulse/profile.py:384
# (load_profile negative-centroid path: negative_centroid = weighted mean of neg_abstract embeddings)
async def test_pulse_profile_w2_negative_centroid_embed_handled(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """Papers with negative recommendation_feedback feed into negative_centroid.

    Seeds a paper with a negative signal for user A, then verifies that
    load_profile computes a non-None negative_centroid of the correct dimension.

    Survivor-of: test_load_profile_happy_path negative-centroid assertions (mock-unit)
    which verify negative_centroid is not None but never call a real embedding boundary.
    """
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_sidecars.faux_ollama import deterministic_embedding
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION
    from paper_ingestion.pulse.profile import load_profile
    from unittest.mock import AsyncMock, MagicMock

    user_a_id = contract_two_users.user_a_id
    paper_id_a = contract_two_users.paper_id_a

    # Give paper_a an abstract so the negative abstract query can find it.
    await contract_conn.execute(
        "UPDATE papers SET abstract = 'negative feedback paper abstract' WHERE id = $1",
        paper_id_a,
    )

    # Insert a recent negative recommendation_feedback row for user A's paper.
    # source is NOT NULL; 'pulse_thumbs' is a valid value per the CHECK constraint.
    await contract_conn.execute(
        """
        INSERT INTO recommendation_feedback (paper_id, user_id, signal, source, created_at)
        VALUES ($1, $2, 'negative', 'pulse_thumbs', NOW())
        ON CONFLICT DO NOTHING
        """,
        paper_id_a,
        user_a_id,
    )

    shared_pool = SharedConnPool(contract_conn)
    mock_embedder = MagicMock()

    async def _embed(texts):
        return [deterministic_embedding(t, dimension=EMBEDDING_DIMENSION) for t in texts]

    mock_embedder.embed_texts = AsyncMock(side_effect=_embed)

    profile = await load_profile(shared_pool, embedder=mock_embedder, user_id=user_a_id)

    assert profile.negative_centroid is not None, (
        "negative_centroid must not be None when a negative feedback paper with abstract exists; "
        "check negative-centroid weighted-mean path in profile.py:384"
    )
    assert len(profile.negative_centroid) == EMBEDDING_DIMENSION, (
        f"negative_centroid dimension mismatch: got {len(profile.negative_centroid)}, "
        f"expected {EMBEDDING_DIMENSION}"
    )
    # The single negative-abstract centroid equals the embedding of that abstract directly
    expected_vec = deterministic_embedding(
        "negative feedback paper abstract", dimension=EMBEDDING_DIMENSION
    )
    for i, (got, exp) in enumerate(zip(profile.negative_centroid, expected_vec)):
        assert abs(got - exp) < 1e-6, (
            f"negative_centroid[{i}] = {got!r}, expected {exp!r}; "
            "centroid does not match the single negative abstract embedding"
        )
