"""Tests for explicit, validated vector-retrieval authorization modes."""

from __future__ import annotations

import pytest

from paper_ingestion.ingestion.search_scope import SearchScope, SearchScopeMode
from paper_ingestion.rag.streaming import (
    _cross_paper_search_scope,
    _requested_cross_paper_ids,
)


def test_caller_corpus_normalizes_private_library_ids() -> None:
    scope = SearchScope.caller_corpus(7, [4, 2, 4])

    assert scope.mode is SearchScopeMode.CALLER_CORPUS
    assert scope.user_id == 7
    assert scope.library_paper_ids == (4, 2)
    assert scope.allowed_paper_ids == ()


def test_explicit_authenticated_scope_keeps_public_request_candidates() -> None:
    scope = _cross_paper_search_scope(
        user_id=7,
        library_paper_ids=[11],
        requested_paper_ids=[11, 22],
    )

    assert scope.mode is SearchScopeMode.EXPLICIT_PAPERS
    assert scope.library_paper_ids == (11,)
    assert scope.allowed_paper_ids == (11, 22)


def test_explicit_internal_scope_needs_no_synthetic_user() -> None:
    scope = SearchScope.explicit_papers(None, [22])

    assert scope.mode is SearchScopeMode.EXPLICIT_PAPERS
    assert scope.user_id is None
    assert scope.allowed_paper_ids == (22,)


def test_requested_ids_are_restrictions_not_library_grants() -> None:
    assert _requested_cross_paper_ids([22, 11, 22]) == [22, 11]
    assert _requested_cross_paper_ids(None) is None


@pytest.mark.parametrize(
    "scope",
    [
        SearchScope(mode=SearchScopeMode.INTERNAL),
        SearchScope(mode=SearchScopeMode.CALLER_CORPUS, user_id=1),
        SearchScope(mode=SearchScopeMode.CALLER_LIBRARY, user_id=1),
    ],
)
def test_non_explicit_modes_reject_requested_ids(scope: SearchScope) -> None:
    with pytest.raises(ValueError, match="Internal search scope|Only explicit-paper"):
        SearchScope(
            mode=scope.mode,
            user_id=scope.user_id,
            allowed_paper_ids=(9,),
        )


def test_caller_mode_requires_user_id() -> None:
    with pytest.raises(ValueError, match="require a user_id"):
        SearchScope(mode=SearchScopeMode.CALLER_CORPUS)


def test_explicit_scope_requires_requested_ids() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SearchScope(mode=SearchScopeMode.EXPLICIT_PAPERS, user_id=1)


def test_scope_rejects_non_positive_paper_ids() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SearchScope.explicit_papers(1, [0])
