"""Visibility-predicate agreement contract test (GC-05).

Two layers decide whether a user may see a paper and they MUST stay in lockstep:

- Single-paper (Python): ``jarvis_common.db_helpers.assert_paper_ownership``
  fast-grants when ``discovered_by`` equals the caller or is NULL (shared
  canonical paper, D4), else runs a ``user_library`` membership fetch and raises
  HTTPException(403) when absent.
- Bulk (SQL): ``paper_ingestion.queries.predicates.paper_visible_sql`` emits only
  the ``discovered_by IS NULL OR discovered_by = $N`` fragment and delegates the
  ``user_library`` membership branch to each call site.

These are intentionally separate (Python runtime membership query vs SQL fragment
+ per-call-site membership), so they can silently drift. This test pins their
agreement across the full 4-cell matrix against real Postgres. Layer B mirrors
the shipped composition in citations.py:_filter_visible_paper_ids (the
``paper_visible_sql(...) OR EXISTS(user_library …)`` shape), not an invented query.

# Verified: libs/jarvis_common/jarvis_common/db_helpers.py:412 (assert_paper_ownership)
# Verified: services/paper_ingestion/paper_ingestion/queries/predicates.py:30 (paper_visible_sql)
# Verified: services/paper_ingestion/paper_ingestion/citations.py:270 (_filter_visible_paper_ids composition)
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from jarvis_common.db_helpers import assert_paper_ownership
from paper_ingestion.queries.predicates import paper_visible_sql

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_user(conn, email: str) -> int:
    """Insert one user; return its id."""
    return int(await conn.fetchval("INSERT INTO users (email) VALUES ($1) RETURNING id", email))


async def _seed_paper(conn, external_id: str, discovered_by: int | None) -> int:
    """Insert one paper with the given ``discovered_by``; return its id."""
    return int(
        await conn.fetchval(
            """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
               VALUES ($1, 'arxiv', 'Visibility paper', ARRAY['A. Author'],
                       'https://example.test/visibility', $2)
               RETURNING id""",
            external_id,
            discovered_by,
        )
    )


async def _add_to_library(conn, user_id: int, paper_id: int) -> None:
    """Place *paper_id* into *user_id*'s library."""
    await conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_id,
        paper_id,
    )


# ---------------------------------------------------------------------------
# Layer probes
# ---------------------------------------------------------------------------


async def _layer_a_visible(conn, paper_id: int, user_id: int) -> bool:
    """Single-paper layer: True iff assert_paper_ownership grants access."""
    try:
        await assert_paper_ownership(conn, paper_id, user_id)
    except HTTPException as exc:
        assert exc.status_code == 403, f"unexpected status from layer A: {exc.status_code}"
        return False
    return True


async def _layer_b_visible(conn, paper_id: int, user_id: int) -> bool:
    """Bulk-SQL layer: True iff *paper_id* survives the shipped visibility filter.

    Mirrors citations.py:_filter_visible_paper_ids — paper_visible_sql(...) OR an
    EXISTS(user_library) membership branch — against the same candidate id.
    """
    membership_sql = f"""
        SELECT papers.id FROM papers
        WHERE papers.id = $1
          AND (
              {paper_visible_sql(2, alias="papers")}
              OR EXISTS (
                  SELECT 1 FROM user_library ul
                  WHERE ul.user_id = $2 AND ul.paper_id = papers.id
              )
          )
    """
    rows = await conn.fetch(membership_sql, paper_id, user_id)
    return len(rows) == 1


async def _assert_layers_agree(
    conn, paper_id: int, user_id: int, expected: bool, cell: str
) -> None:
    """Both layers must return *expected* for (paper_id, user_id)."""
    a = await _layer_a_visible(conn, paper_id, user_id)
    b = await _layer_b_visible(conn, paper_id, user_id)
    assert a == b, (
        f"[{cell}] layers disagree: assert_paper_ownership={a!r}, paper_visible_sql={b!r}"
    )
    assert a == expected, f"[{cell}] expected visible={expected!r}, both layers gave {a!r}"


# ---------------------------------------------------------------------------
# 4-cell agreement matrix
# ---------------------------------------------------------------------------


async def test_owned_paper_visible_to_caller(contract_conn):
    """discovered_by == caller → VISIBLE in both layers."""
    caller = await _seed_user(contract_conn, "vis-owner@contract.example.com")
    paper = await _seed_paper(contract_conn, "vis-owned", caller)
    await _assert_layers_agree(contract_conn, paper, caller, expected=True, cell="owned-by-caller")


async def test_shared_null_paper_visible_to_caller(contract_conn):
    """discovered_by IS NULL (shared canonical) → VISIBLE in both layers, no library row."""
    caller = await _seed_user(contract_conn, "vis-shared@contract.example.com")
    paper = await _seed_paper(contract_conn, "vis-shared", None)
    await _assert_layers_agree(contract_conn, paper, caller, expected=True, cell="shared-null")


async def test_other_owned_paper_in_library_visible(contract_conn):
    """discovered_by == other user, but in caller's library → VISIBLE in both layers."""
    caller = await _seed_user(contract_conn, "vis-lib-caller@contract.example.com")
    other = await _seed_user(contract_conn, "vis-lib-other@contract.example.com")
    paper = await _seed_paper(contract_conn, "vis-other-in-lib", other)
    await _add_to_library(contract_conn, caller, paper)
    await _assert_layers_agree(
        contract_conn, paper, caller, expected=True, cell="other-owned-in-library"
    )


async def test_other_owned_paper_not_in_library_invisible(contract_conn):
    """discovered_by == other user, not in caller's library → NOT VISIBLE in both layers."""
    caller = await _seed_user(contract_conn, "vis-nolib-caller@contract.example.com")
    other = await _seed_user(contract_conn, "vis-nolib-other@contract.example.com")
    paper = await _seed_paper(contract_conn, "vis-other-not-in-lib", other)
    await _assert_layers_agree(
        contract_conn, paper, caller, expected=False, cell="other-owned-not-in-library"
    )


# ---------------------------------------------------------------------------
# Auto-summary holder selection (auto_fetch._UNSUMMARIZED_HOLDERS_SQL)
#
# Discovery defers one paper.summarize per library holder that lacks a summary
# OF THEIR OWN. Summaries are per-user by schema and every reader binds a strict
# integer owner, so the selection's correlated NOT EXISTS must key on BOTH
# paper_id and user_id. A paper-global check (the shipped bug this replaces)
# silently starves every holder after the first. Mocks cannot tell a correct
# correlation from a subtly wrong one, so these run the shipped SQL constant
# against real Postgres.
#
# Verified: services/paper_ingestion/paper_ingestion/pipelines/auto_fetch.py
#           (_UNSUMMARIZED_HOLDERS_SQL — imported here, never re-typed)
# ---------------------------------------------------------------------------


async def _seed_summary(conn, paper_id: int, user_id: int) -> None:
    """Give *user_id* their own summary row for *paper_id*."""
    await conn.execute(
        """INSERT INTO paper_summaries (paper_id, user_id, summary_brief, summary_detailed)
           VALUES ($1, $2, 'brief', 'detailed')""",
        paper_id,
        user_id,
    )


async def _unsummarized_holders(conn, paper_id: int) -> set[int]:
    """Run the shipped holder-selection query; return the selected user ids."""
    from paper_ingestion.pipelines.auto_fetch import _UNSUMMARIZED_HOLDERS_SQL

    rows = await conn.fetch(_UNSUMMARIZED_HOLDERS_SQL, paper_id)
    return {int(row["user_id"]) for row in rows}


async def test_holder_without_any_summary_is_selected(contract_conn):
    """A library holder with no summary at all is selected for summarization."""
    holder = await _seed_user(contract_conn, "sum-plain-holder@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-plain", None)
    await _add_to_library(contract_conn, holder, paper)

    assert await _unsummarized_holders(contract_conn, paper) == {holder}


async def test_holder_with_own_summary_is_not_selected(contract_conn):
    """A holder who already has THEIR OWN summary is skipped — no redundant LLM spend."""
    holder = await _seed_user(contract_conn, "sum-own-holder@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-own", None)
    await _add_to_library(contract_conn, holder, paper)
    await _seed_summary(contract_conn, paper, holder)

    assert await _unsummarized_holders(contract_conn, paper) == set()


async def test_holder_still_selected_when_a_different_user_has_a_summary(contract_conn):
    """The regression case: user A's summary must NOT suppress holder B.

    A paper-global EXISTS(paper_summaries WHERE paper_id = $1) — the shipped bug —
    returns zero holders here, leaving B with a summary they can never read.
    """
    summarized = await _seed_user(contract_conn, "sum-cross-a@contract.example.com")
    pending = await _seed_user(contract_conn, "sum-cross-b@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-cross", None)
    await _add_to_library(contract_conn, summarized, paper)
    await _add_to_library(contract_conn, pending, paper)
    await _seed_summary(contract_conn, paper, summarized)

    selected = await _unsummarized_holders(contract_conn, paper)
    assert pending in selected, (
        "holder B has no summary of their own and MUST still be selected; "
        "a paper-global summary check would starve them"
    )
    assert summarized not in selected


async def test_non_holder_is_never_selected_even_with_a_summary_row(contract_conn):
    """Selection is driven by user_library membership, never by paper_summaries.

    A user with a summary row but no library entry must not be re-summarized.
    """
    holder = await _seed_user(contract_conn, "sum-nonholder-holder@contract.example.com")
    stranger = await _seed_user(contract_conn, "sum-nonholder-stranger@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-nonholder", None)
    await _add_to_library(contract_conn, holder, paper)
    await _seed_summary(contract_conn, paper, stranger)

    assert await _unsummarized_holders(contract_conn, paper) == {holder}
