"""Unit contracts for the centralized paper-visibility policy."""

from __future__ import annotations

import pytest


def test_visibility_sql_uses_persisted_scope_or_library_membership() -> None:
    """The pure builder returns the complete canonical visibility predicate."""
    from jarvis_common.paper_visibility import paper_visibility_sql

    sql = paper_visibility_sql(2, alias="papers")
    expected = (
        "(papers.visibility_scope = 'public' OR EXISTS ("
        "SELECT 1 FROM user_library visibility_ul"
        " WHERE visibility_ul.user_id = $2"
        " AND visibility_ul.paper_id = papers.id"
        "))"
    )

    assert sql == expected


@pytest.mark.parametrize("source_type", ["arxiv", "semantic_scholar", "openalex", "pubmed"])
def test_verified_public_source_guard_accepts_only_server_adapter_set(source_type: str) -> None:
    """Known scholarly adapters are eligible for explicit server-owned promotion."""
    from jarvis_common.paper_visibility import require_verified_public_source

    require_verified_public_source(source_type)


@pytest.mark.parametrize(
    "source_type",
    ["local", "zotero", "citation_batch", "unknown", "", "ARXIV"],
)
def test_verified_public_source_guard_rejects_untrusted_or_ambiguous_values(
    source_type: str,
) -> None:
    """Client-controlled, private, unknown, blank, and non-canonical labels fail closed."""
    from jarvis_common.paper_visibility import require_verified_public_source

    with pytest.raises(ValueError, match="not eligible for public visibility"):
        require_verified_public_source(source_type)


def test_visibility_scope_constants_are_closed_and_immutable() -> None:
    """The shared source and scope sets expose no mutable policy surface."""
    from jarvis_common.paper_visibility import (
        PRIVATE_VISIBILITY_SCOPE,
        PUBLIC_VISIBILITY_SCOPE,
        VERIFIED_PUBLIC_SOURCE_TYPES,
    )

    assert PUBLIC_VISIBILITY_SCOPE == "public"
    assert PRIVATE_VISIBILITY_SCOPE == "private"
    assert VERIFIED_PUBLIC_SOURCE_TYPES == frozenset(
        {"arxiv", "semantic_scholar", "openalex", "pubmed"}
    )
