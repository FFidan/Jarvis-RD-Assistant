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
