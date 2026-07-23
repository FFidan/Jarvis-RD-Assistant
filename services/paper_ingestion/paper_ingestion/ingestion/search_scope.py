"""Validated visibility modes shared by Qdrant retrieval callers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SearchScopeMode(StrEnum):
    """Mutually exclusive vector-retrieval authorization modes."""

    INTERNAL = "internal"
    CALLER_LIBRARY = "caller_library"
    CALLER_CORPUS = "caller_corpus"
    EXPLICIT_PAPERS = "explicit_papers"


@dataclass(frozen=True, slots=True)
class SearchScope:
    """Validated authorization scope for one vector query.

    Construct instances through the class methods so internal, library,
    corpus, and explicit-paper modes cannot be combined accidentally.
    """

    mode: SearchScopeMode
    user_id: int | None = None
    library_paper_ids: tuple[int, ...] = ()
    allowed_paper_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.mode is SearchScopeMode.INTERNAL:
            if self.user_id is not None or self.library_paper_ids or self.allowed_paper_ids:
                raise ValueError("Internal search scope cannot carry caller restrictions")
            return
        if self.user_id is None and self.mode is not SearchScopeMode.EXPLICIT_PAPERS:
            raise ValueError("Caller search scopes require a user_id")
        if self.mode is SearchScopeMode.EXPLICIT_PAPERS and not self.allowed_paper_ids:
            raise ValueError("Explicit-paper search scope requires at least one paper ID")
        if self.mode is not SearchScopeMode.EXPLICIT_PAPERS and self.allowed_paper_ids:
            raise ValueError("Only explicit-paper scope may carry allowed_paper_ids")

    @classmethod
    def internal(cls) -> SearchScope:
        """Build the trusted system scope with no authorization filter.

        Returns
        -------
        SearchScope
            Internal mode without caller or paper restrictions.

        Notes
        -----
        Only trusted service-owned operations may use this factory. HTTP and
        other user-reachable callers must construct a caller scope instead.
        """
        return cls(mode=SearchScopeMode.INTERNAL)

    @classmethod
    def caller_library(
        cls,
        user_id: int,
        paper_ids: list[int] | tuple[int, ...],
    ) -> SearchScope:
        """Return exact caller-library scope.

        Parameters
        ----------
        user_id : int
            Authenticated caller identifier.
        paper_ids : list[int] | tuple[int, ...]
            Exact paper IDs loaded from that caller's ``user_library``.

        Returns
        -------
        SearchScope
            Caller-library mode with de-duplicated paper IDs.

        Raises
        ------
        ValueError
            If any supplied paper ID is not positive.
        """
        return cls(
            mode=SearchScopeMode.CALLER_LIBRARY,
            user_id=user_id,
            library_paper_ids=_normalized_ids(paper_ids),
        )

    @classmethod
    def caller_corpus(
        cls,
        user_id: int,
        private_paper_ids: list[int] | tuple[int, ...] = (),
    ) -> SearchScope:
        """Build persisted-public plus caller-library-private scope.

        Parameters
        ----------
        user_id : int
            Authenticated caller identifier.
        private_paper_ids : list[int] | tuple[int, ...]
            Private paper IDs loaded from that caller's ``user_library``.

        Returns
        -------
        SearchScope
            Caller-corpus mode carrying only the caller's private memberships;
            persisted public rows are admitted by the shared vector filter.

        Raises
        ------
        ValueError
            If any supplied paper ID is not positive.
        """
        return cls(
            mode=SearchScopeMode.CALLER_CORPUS,
            user_id=user_id,
            library_paper_ids=_normalized_ids(private_paper_ids),
        )

    @classmethod
    def explicit_papers(
        cls,
        user_id: int | None,
        requested_paper_ids: list[int] | tuple[int, ...],
        private_paper_ids: list[int] | tuple[int, ...] = (),
    ) -> SearchScope:
        """Return an explicit paper restriction with optional caller policy.

        Parameters
        ----------
        user_id : int | None
            Authenticated caller whose persisted-public/library policy must
            also hold. ``None`` is reserved for trusted internal callers.
        requested_paper_ids : list[int] | tuple[int, ...]
            Exact papers permitted by the request or internal operation.
        private_paper_ids : list[int] | tuple[int, ...]
            Exact private-paper memberships for an authenticated caller.

        Returns
        -------
        SearchScope
            Explicit-paper mode intersecting the requested IDs with caller
            visibility when ``user_id`` is present.

        Raises
        ------
        ValueError
            If the request is empty or any supplied paper ID is not positive.

        Notes
        -----
        ``user_id=None`` is reserved for trusted service-owned operations. A
        user-reachable caller must supply its authenticated user ID and private
        memberships.
        """
        return cls(
            mode=SearchScopeMode.EXPLICIT_PAPERS,
            user_id=user_id,
            library_paper_ids=_normalized_ids(private_paper_ids),
            allowed_paper_ids=_normalized_ids(requested_paper_ids),
        )


def _normalized_ids(values: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    """Return unique positive paper IDs while preserving input order."""
    normalized = tuple(dict.fromkeys(int(value) for value in values))
    if any(value <= 0 for value in normalized):
        raise ValueError("Paper IDs in a search scope must be positive")
    return normalized
