"""Sprint B — feed query semantics under canonical-corpus + user_library."""

from __future__ import annotations

import pytest
from paper_ingestion.services.feed_query import build_feed_queries


def _build(user_id: int | None):
    return build_feed_queries(
        unread_only=False,
        sort="discovered_at",
        limit=10,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=user_id,
    )


def test_user_id_present_uses_library_join():
    parts = _build(user_id=42)
    assert "JOIN user_library ul" in parts.data_query
    assert "ul.user_id = $1" in parts.data_query
    # Legacy predicate must be gone.
    assert "p.user_id IS NULL" not in parts.data_query
    # The renamed audit column must NOT leak into feed scoping either.
    assert "p.discovered_by" not in parts.data_query


def test_user_id_none_falls_back_to_canonical_corpus():
    parts = _build(user_id=None)
    assert "JOIN user_library" not in parts.data_query
    # The fallback FROM clause is still rooted on `papers p`.
    assert " FROM papers p" in parts.data_query


def test_user_a_and_user_b_get_disjoint_param_lists():
    """Two callers building queries against the same builder produce
    queries differing only in the bound user_id parameter."""
    a = _build(user_id=1)
    b = _build(user_id=2)
    assert a.data_query == b.data_query  # SQL is parametric
    assert a.params[0] == 1
    assert b.params[0] == 2


def test_count_query_also_uses_library_join_when_user_id_set():
    parts = _build(user_id=99)
    assert "JOIN user_library" in parts.count_query
    assert "ul.user_id = $1" in parts.count_query


@pytest.mark.parametrize("uid", [None, 42])
def test_param_layout_starts_with_user_id_at_dollar1(uid):
    """The first parameter is always user_id (or its None placeholder for
    fallback queries) so the LEFT JOIN onto paper_user_state can keep
    binding $1."""
    parts = _build(user_id=uid)
    assert parts.params[0] is uid
