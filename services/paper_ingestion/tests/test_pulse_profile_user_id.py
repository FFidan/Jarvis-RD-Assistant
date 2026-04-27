"""Tests for load_profile() user_id filtering (H20 / WS-6C).

Verifies that:
- When user_id is provided, the rating SQL contains the IS NOT DISTINCT FROM filter.
- When user_id is None, no user_id filter is applied (single-user passthrough).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from paper_ingestion.pulse.profile import UserProfile, load_profile


@pytest.mark.asyncio
async def test_load_profile_with_user_id_filters_ratings():
    """When user_id=42 is passed, the positive-rating SQL must include
    'IS NOT DISTINCT FROM' and bind 42 as a parameter."""
    # Phase 1 connection: topics, authors, engaged papers
    phase1_conn = AsyncMock()
    phase1_conn.fetch.side_effect = [
        [],  # topics
        [],  # tracked_authors
        [],  # engaged papers for centroid
    ]
    # Phase 3 connection: config + ratings
    phase3_conn = AsyncMock()
    phase3_conn.fetch.side_effect = [
        [{"key": "pulse.deck_size", "value": 10}],  # user_config
        [{"id": 1, "title": "Liked Paper"}],  # positive ratings
        [{"title": "Disliked Paper"}],  # negative ratings
    ]

    pool = MagicMock()
    # First acquire() → phase1_conn, second → phase3_conn
    acquire_ctx_1 = MagicMock()
    acquire_ctx_1.__aenter__ = AsyncMock(return_value=phase1_conn)
    acquire_ctx_1.__aexit__ = AsyncMock(return_value=False)
    acquire_ctx_2 = MagicMock()
    acquire_ctx_2.__aenter__ = AsyncMock(return_value=phase3_conn)
    acquire_ctx_2.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.side_effect = [acquire_ctx_1, acquire_ctx_2]

    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.return_value = []

    profile = await load_profile(pool, embedder=mock_embedder, user_id=42)

    assert isinstance(profile, UserProfile)

    # The third and fourth fetch calls on phase3_conn are the positive and negative
    # rating queries.  When user_id=42 is passed, they must bind 42 as a parameter.
    fetch_calls = phase3_conn.fetch.call_args_list
    # call index 1 = positive ratings query, call index 2 = negative ratings query
    assert len(fetch_calls) >= 3, "Expected config + positive + negative fetch calls"

    positive_call = fetch_calls[1]
    positive_sql = positive_call[0][0]  # first positional arg to fetch()
    positive_params = list(positive_call[0][1:])  # remaining positional args

    assert "IS NOT DISTINCT FROM" in positive_sql, (
        f"Expected 'IS NOT DISTINCT FROM' in positive-rating SQL, got: {positive_sql!r}"
    )
    assert 42 in positive_params, (
        f"Expected user_id=42 in positive-rating query params, got: {positive_params}"
    )

    negative_call = fetch_calls[2]
    negative_sql = negative_call[0][0]
    negative_params = list(negative_call[0][1:])

    assert "IS NOT DISTINCT FROM" in negative_sql, (
        f"Expected 'IS NOT DISTINCT FROM' in negative-rating SQL, got: {negative_sql!r}"
    )
    assert 42 in negative_params, (
        f"Expected user_id=42 in negative-rating query params, got: {negative_params}"
    )


@pytest.mark.asyncio
async def test_load_profile_without_user_id_no_filter():
    """When user_id=None (default), rating SQL must NOT contain IS NOT DISTINCT FROM."""
    phase1_conn = AsyncMock()
    phase1_conn.fetch.side_effect = [
        [],  # topics
        [],  # tracked_authors
        [],  # engaged papers
    ]
    phase3_conn = AsyncMock()
    phase3_conn.fetch.side_effect = [
        [],  # user_config
        [],  # positive ratings
        [],  # negative ratings
    ]

    pool = MagicMock()
    acquire_ctx_1 = MagicMock()
    acquire_ctx_1.__aenter__ = AsyncMock(return_value=phase1_conn)
    acquire_ctx_1.__aexit__ = AsyncMock(return_value=False)
    acquire_ctx_2 = MagicMock()
    acquire_ctx_2.__aenter__ = AsyncMock(return_value=phase3_conn)
    acquire_ctx_2.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.side_effect = [acquire_ctx_1, acquire_ctx_2]

    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.return_value = []

    profile = await load_profile(pool, embedder=mock_embedder, user_id=None)

    assert isinstance(profile, UserProfile)

    fetch_calls = phase3_conn.fetch.call_args_list
    assert len(fetch_calls) >= 3

    positive_sql = fetch_calls[1][0][0]
    assert "IS NOT DISTINCT FROM" not in positive_sql, (
        "user_id=None should NOT add IS NOT DISTINCT FROM filter to positive-rating SQL"
    )

    negative_sql = fetch_calls[2][0][0]
    assert "IS NOT DISTINCT FROM" not in negative_sql, (
        "user_id=None should NOT add IS NOT DISTINCT FROM filter to negative-rating SQL"
    )
