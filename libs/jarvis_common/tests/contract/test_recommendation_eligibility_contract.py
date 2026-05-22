"""Recommendation eligibility contract suite (predicate rows A255, A280).

Exercises ``recommender._filter_unread`` against a real DB to prove the
state-based exclusion predicates:

  - inbox (no user_state row) → eligible  (control)
  - trash state → excluded
  - done state → excluded
  - negative feedback within 60 days → excluded
  - negative feedback older than 60 days → eligible (60d window boundary)

This is the shared parametrized suite covering the predicate directly.
B.PI-pulse-rag covers the /api/recommendations route integration; this suite
covers the SQL predicate in isolation via function import (no HTTP layer),
making it faster and more precise.

Grounding (recommender.py:220-245, read at HEAD):
  ``_filter_unread`` executes a single SELECT with two NOT EXISTS subqueries:
  1. paper_user_state: COALESCE(state,'inbox') IN ('trash','done')
  2. recommendation_feedback: signal='negative' AND created_at > NOW()-'60 days'
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


# ---------------------------------------------------------------------------
# Parametrised cases
# ---------------------------------------------------------------------------

# (state_or_feedback, feedback_age_days, expected_eligible)
# state_or_feedback values:
#   None          — no paper_user_state row (natural "inbox")
#   "inbox"       — explicit inbox state
#   "trash"       — trash state → excluded
#   "done"        — done state → excluded
#   "neg_recent"  — negative feedback within 60d → excluded
#   "neg_old"     — negative feedback older than 60d → eligible
_CASES = [
    ("no_state", None, True),
    ("inbox", None, True),
    ("trash", None, False),
    ("done", None, False),
    ("neg_recent", 30, False),
    ("neg_old", 61, True),
]

_CASE_IDS = [c[0] for c in _CASES]


async def _setup_paper_and_user(conn) -> tuple[int, int]:
    """Seed a minimal paper + user pair; return (paper_id, user_id)."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        f"eligib-{id(conn)}@contract.test",
    )
    paper_id = await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'Eligibility Test Paper', ARRAY['A. Author'],
                   'https://example.test/e', $2)
           RETURNING id""",
        f"eligib-ext-{id(conn)}",
        user_id,
    )
    return int(paper_id), int(user_id)


@pytest.mark.parametrize("scenario,feedback_days,expected_eligible", _CASES, ids=_CASE_IDS)
async def test_filter_unread_eligibility(
    scenario: str,
    feedback_days: int | None,
    expected_eligible: bool,
    contract_conn,
) -> None:
    """``_filter_unread`` includes / excludes paper per state / feedback predicate.

    Uses the real SQL predicate against the contract DB (no mocks).
    Covers predicate rows A255 (state exclusion) and A280 (60d feedback window).
    """
    from paper_ingestion.ingestion.recommender import _filter_unread  # noqa: PLC0415

    paper_id, user_id = await _setup_paper_and_user(contract_conn)

    if scenario in ("inbox", "trash", "done"):
        await contract_conn.execute(
            """INSERT INTO paper_user_state (paper_id, user_id, state)
               VALUES ($1, $2, $3)""",
            paper_id,
            user_id,
            scenario,
        )
    elif scenario in ("neg_recent", "neg_old"):
        assert feedback_days is not None
        created_at = datetime.now(UTC) - timedelta(days=feedback_days)
        await contract_conn.execute(
            """INSERT INTO recommendation_feedback
               (paper_id, user_id, signal, source, created_at)
               VALUES ($1, $2, 'negative', 'feed_thumbs', $3)""",
            paper_id,
            user_id,
            created_at,
        )
    # else: "no_state" — no row inserted; COALESCE defaults to 'inbox' → eligible

    result = await _filter_unread(contract_conn, [paper_id], user_id)

    if expected_eligible:
        assert paper_id in result, (
            f"Scenario '{scenario}': paper_id {paper_id} should be ELIGIBLE "
            f"(expected in result set {result})"
        )
    else:
        assert paper_id not in result, (
            f"Scenario '{scenario}': paper_id {paper_id} should be EXCLUDED "
            f"(should not be in result set {result})"
        )
