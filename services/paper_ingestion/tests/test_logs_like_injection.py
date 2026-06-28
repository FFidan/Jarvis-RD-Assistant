"""LIKE metacharacter escaping for the /api/logs/events search.

A user-supplied ``q`` flows into a ``message ILIKE`` predicate that the endpoint
builds as ``f"%{escape_like(q)}%"`` paired with ``ESCAPE '\\'`` (see
``routers/logs.py``). Without the escape, a term of ``'%'`` becomes a match-all
wildcard (information disclosure / full-table-scan DoS) and ``'_'`` matches any
single character. This pins the escaping helper that neutralises those
metacharacters into literals.
"""

from __future__ import annotations

import pytest

from jarvis_common.db_helpers import escape_like


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("%", r"\%"),  # match-all wildcard -> literal percent
        ("_", r"\_"),  # single-char wildcard -> literal underscore
        ("a_b", r"a\_b"),
        ("100%", r"100\%"),
        ("\\", r"\\"),  # backslash escaped first (no double-escape)
        ("plain text", "plain text"),
    ],
)
def test_escape_like_neutralizes_like_metacharacters(raw: str, expected: str) -> None:
    assert escape_like(raw) == expected
