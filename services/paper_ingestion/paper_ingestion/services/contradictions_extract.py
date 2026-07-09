"""Data loading + dataclasses + lexical primitives for contradiction scanning.

Pure functions: parses persisted summary findings into ``VerifiedFinding``
records, plus the lexical helpers (``_terms``, ``_jaccard``) and polarity-cue
constants used by the scoring path in ``contradictions.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import asyncpg
from jarvis_common.prompt_safety import wrap_delimited

from paper_ingestion.db_types import ConnLike

_STOP_WORDS = {
    "about",
    "after",
    "also",
    "among",
    "from",
    "have",
    "into",
    "paper",
    "result",
    "results",
    "show",
    "shows",
    "study",
    "that",
    "their",
    "there",
    "these",
    "this",
    "using",
    "with",
}
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_POSITIVE_CUES = {
    "better",
    "benefit",
    "beneficial",
    "boost",
    "effective",
    "higher",
    "improve",
    "improved",
    "improves",
    "improvement",
    "increase",
    "increased",
    "increases",
    "outperform",
    "outperformed",
    "outperforms",
    "positive",
    "reduces error",
    "significant",
    "supports",
    "works",
}
_NEGATIVE_CUES = {
    "decrease",
    "decreased",
    "decreases",
    "harm",
    "harmful",
    "lower",
    "negative",
    "no benefit",
    "no improvement",
    "not significant",
    "reduce",
    "reduced",
    "reduces",
    "worse",
    "worsen",
    "worsened",
    "worsens",
}
# Build word-boundary regexes with longest-first alternation to prevent shorter cues
# (e.g. "reduces") from shadowing longer multi-word cues (e.g. "reduces error").
_POSITIVE_RE = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(c) for c in sorted(_POSITIVE_CUES, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)
_NEGATIVE_RE_CUES = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(c) for c in sorted(_NEGATIVE_CUES, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VerifiedFinding:
    """A quote-verified summary finding used as scanner input."""

    paper_id: int
    title: str
    finding: str
    quote: str
    page_number: int | None
    cross_reference_ids: frozenset[int]


@dataclass(frozen=True)
class ContradictionCandidate:
    """A narrowed pair of findings worth sending to the classifier."""

    a: VerifiedFinding
    b: VerifiedFinding
    score: float
    reason: str


def _is_term(word: str) -> bool:
    lowered = word.lower()
    if lowered in _STOP_WORDS:
        return False
    if len(word) > 3:
        return True
    return len(word) == 3 and word.isupper()


def _terms(text: str) -> set[str]:
    return {word.lower() for word in _WORD_RE.findall(text) if _is_term(word)}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Return lexical-set similarity as a cheap semantic proxy."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _json_list(raw: Any) -> list[Any]:
    """Return *raw* as a list, accepting JSON strings from DB fixtures."""

    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _cross_reference_ids(raw: Any) -> frozenset[int]:
    ids: set[int] = set()
    for item in _json_list(raw):
        if isinstance(item, dict) and isinstance(item.get("related_paper_id"), int):
            ids.add(item["related_paper_id"])
    return frozenset(ids)


def _parse_findings(row: asyncpg.Record) -> list[VerifiedFinding]:
    raw_findings = _json_list(row["key_findings"] or [])
    cross_reference_ids = _cross_reference_ids(row["cross_references"] or [])
    parsed: list[VerifiedFinding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        if item.get("verified") is False:
            continue
        finding = str(item.get("finding") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not finding or not quote:
            continue
        parsed.append(
            VerifiedFinding(
                paper_id=row["paper_id"],
                title=row["title"],
                finding=finding,
                quote=quote,
                page_number=item.get("page_number")
                if isinstance(item.get("page_number"), int)
                else None,
                cross_reference_ids=cross_reference_ids,
            )
        )
    return parsed


# Shared scope for scannable summary rows: non-empty key_findings, optional
# paper focus ($1 = paper_id), and the caller's tenancy scope ($2 = user_id).
# Used by both the findings loader and the cheap preflight COUNT below so the
# two predicates can never drift apart.
_SCANNABLE_SUMMARIES_SQL = """
        FROM paper_summaries ps
        JOIN papers p ON p.id = ps.paper_id
        WHERE CASE
                  WHEN jsonb_typeof(ps.key_findings) = 'array'
                  THEN jsonb_array_length(ps.key_findings)
                  ELSE 0
              END > 0
          AND ($1::integer IS NULL OR p.id = $1 OR EXISTS (
              SELECT 1
              FROM jsonb_array_elements(
                  CASE
                      WHEN jsonb_typeof(ps.cross_references) = 'array'
                      THEN ps.cross_references
                      ELSE '[]'::jsonb
                  END
              ) AS ref
              WHERE ref->>'related_paper_id' ~ '^[0-9]+$'
                AND (ref->>'related_paper_id')::integer = $1
          ))
          AND ($2::integer IS NULL OR ps.user_id IS NULL OR ps.user_id = $2)
          AND ($2::integer IS NULL OR EXISTS (
              SELECT 1
              FROM user_library ul
              WHERE ul.paper_id = p.id AND ul.user_id = $2
          ))
"""


async def _load_verified_findings(
    conn: ConnLike,
    *,
    paper_id: int | None = None,
    user_id: int | None = None,
) -> list[VerifiedFinding]:
    """Load verified findings, scoped to the caller's library when available."""
    rows = await conn.fetch(
        f"""
        SELECT p.id AS paper_id, p.title, ps.key_findings, ps.cross_references
        {_SCANNABLE_SUMMARIES_SQL}
        ORDER BY ps.created_at DESC
        LIMIT 250
        """,
        paper_id,
        user_id,
    )
    findings: list[VerifiedFinding] = []
    for row in rows:
        findings.extend(_parse_findings(row))
    return findings


async def count_scannable_summaries(conn: ConnLike, *, user_id: int) -> int:
    """Cheap preflight count of summary rows a library-wide scan would load.

    Mirrors ``_load_verified_findings``'s scope via the shared SQL fragment
    without fetching row payloads. Zero rows means any scan for this caller is
    guaranteed to produce no candidates, so the router can skip queuing a job.
    """
    count = await conn.fetchval(f"SELECT COUNT(*) {_SCANNABLE_SUMMARIES_SQL}", None, user_id)
    return int(count or 0)


def _build_prompt(candidate: ContradictionCandidate) -> str:
    title_a, _ = wrap_delimited("title_a", candidate.a.title)
    finding_a, _ = wrap_delimited("finding_a", candidate.a.finding)
    quote_a, _ = wrap_delimited("quote_a", candidate.a.quote)
    title_b, _ = wrap_delimited("title_b", candidate.b.title)
    finding_b, _ = wrap_delimited("finding_b", candidate.b.finding)
    quote_b, _ = wrap_delimited("quote_b", candidate.b.quote)
    return f"""\
Paper A:
{title_a}
{finding_a}
{quote_a}

Paper B:
{title_b}
{finding_b}
{quote_b}
"""
