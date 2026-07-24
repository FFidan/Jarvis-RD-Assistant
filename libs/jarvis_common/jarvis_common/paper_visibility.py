"""Central paper-visibility policy shared by database-backed services.

Paper provenance is descriptive metadata. Authorization depends only on the
persisted visibility scope and explicit user-library membership. Public scope
is assigned through a separate, server-owned ingestion boundary.
"""

from __future__ import annotations

from typing import Literal

PUBLIC_VISIBILITY_SCOPE = "public"
PRIVATE_VISIBILITY_SCOPE = "private"
VisibilityScope = Literal["public", "private"]

VERIFIED_PUBLIC_SOURCE_TYPES = frozenset({"arxiv", "semantic_scholar", "openalex", "pubmed"})


def require_verified_public_source(source_type: str) -> None:
    """Reject a source that is not eligible for server-owned public promotion.

    Parameters
    ----------
    source_type : str
        Canonical source-adapter identifier from a trusted server ingestion
        path. Request-supplied descriptive labels must not be passed here.

    Raises
    ------
    ValueError
        If the source is private, unknown, blank, or not in the verified
        scholarly adapter set.
    """
    if source_type not in VERIFIED_PUBLIC_SOURCE_TYPES:
        raise ValueError(f"source type {source_type!r} is not eligible for public visibility")


def paper_visibility_sql(user_param: int, *, alias: str = "p") -> str:
    """Build the canonical authenticated paper-visibility SQL predicate.

    Parameters
    ----------
    user_param : int
        One-based PostgreSQL placeholder index containing the caller's user ID.
    alias : str
        Trusted SQL alias for the `papers` relation.

    Returns
    -------
    str
        A parenthesized predicate that grants access to persisted public rows
        or rows explicitly present in the caller's `user_library`.

    Notes
    -----
    The caller supplies the alias from static application SQL. Neither
    `source_type` nor `discovered_by` participates in authorization.
    """
    return (
        f"({alias}.visibility_scope = '{PUBLIC_VISIBILITY_SCOPE}'"
        " OR EXISTS ("
        "SELECT 1 FROM user_library visibility_ul"
        f" WHERE visibility_ul.user_id = ${user_param}"
        f" AND visibility_ul.paper_id = {alias}.id"
        "))"
    )
