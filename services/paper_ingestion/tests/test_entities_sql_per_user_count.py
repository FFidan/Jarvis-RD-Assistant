"""Live-PG test: KG min-paper-count filter uses per-user mention counts.

Guard against the global ``entities.paper_count`` being used instead of the
per-user count from ``paper_entities``.  The bug: with ``min_paper_count=2``
and entity mentioned by two users (A once, B twice), the global count is 3 so
BOTH users see the entity — but only B has enough personal mentions to meet the
threshold.

Uses the contract-layer session-scoped pool + per-test txn-rollback fixture so
each test sees a clean DB slice without spawning a new container.

Gated by ``JARVIS_RUN_LIVE_PG=1`` (contract tests use the same live PG).
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Helper: insert a paper with a given discovered_by user_id.
# ---------------------------------------------------------------------------


async def _seed_paper(conn, external_id: str, discovered_by: int) -> int:
    return await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'F15 Test Paper', ARRAY['Author'],
                  'https://f15.test/paper', $2)
           RETURNING id""",
        external_id,
        discovered_by,
    )


# ---------------------------------------------------------------------------
# Main guard: B's count is what matters, not the global total.
# ---------------------------------------------------------------------------


async def test_min_count_uses_per_user_count_not_global(
    contract_two_users,
    contract_conn,
):
    """With min_paper_count=2, only user B (2 mentions) sees the entity.

    Setup:
    - A shared entity.  Global paper_count=3 (A: 1, B: 2).
    - min_paper_count=2.
    Expected:
    - user A (per-user count = 1) → entity EXCLUDED from A's KG.
    - user B (per-user count = 2) → entity INCLUDED in B's KG.

    Before the fix, both A and B would see the entity because the filter used
    the global ``e.paper_count`` (= 3) instead of the per-user count.
    """
    from paper_ingestion.extraction.entities_sql import get_knowledge_graph

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    # Seed the shared entity.
    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('f15-shared-entity', 'f15-shared-entity', 'concept', 0)
           RETURNING id"""
    )

    # User A: one paper → one paper_entities row.
    paper_a = await _seed_paper(contract_conn, "f15-paper-a", user_a_id)
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, mention_count, user_id)
           VALUES ($1, $2, 1, $3)
           ON CONFLICT (paper_id, entity_id, user_id) DO NOTHING""",
        paper_a,
        entity_id,
        user_a_id,
    )

    # User B: two separate papers → two paper_entities rows (per-user count = 2).
    paper_b1 = await _seed_paper(contract_conn, "f15-paper-b1", user_b_id)
    paper_b2 = await _seed_paper(contract_conn, "f15-paper-b2", user_b_id)
    for paper_id in (paper_b1, paper_b2):
        await contract_conn.execute(
            """INSERT INTO paper_entities (paper_id, entity_id, mention_count, user_id)
               VALUES ($1, $2, 1, $3)
               ON CONFLICT (paper_id, entity_id, user_id) DO NOTHING""",
            paper_id,
            entity_id,
            user_b_id,
        )

    # Update the denormalized global paper_count to reflect 3 total rows.
    await contract_conn.execute(
        "UPDATE entities SET paper_count = 3 WHERE id = $1",
        entity_id,
    )

    # --- ACT ---
    result_a = await get_knowledge_graph(contract_conn, min_paper_count=2, user_id=user_a_id)
    result_b = await get_knowledge_graph(contract_conn, min_paper_count=2, user_id=user_b_id)

    names_a = {e["name"] for e in result_a["entities"]}
    names_b = {e["name"] for e in result_b["entities"]}

    # User A's per-user count is 1 — below the threshold of 2 → must be excluded.
    assert "f15-shared-entity" not in names_a, (
        "BUG: user A (per-user count=1) must not see the entity at min_paper_count=2; "
        f"global count=3 leaking through. names_a={names_a}"
    )

    # User B's per-user count is 2 — meets the threshold → must be included.
    assert "f15-shared-entity" in names_b, (
        f"User B (per-user count=2) must see the entity at min_paper_count=2; names_b={names_b}"
    )


async def test_min_count_with_entity_type_filter_uses_per_user_count(
    contract_two_users,
    contract_conn,
):
    """Same guard but via the entity_type-filtered branch (entities_sql.py:113-124).

    Exercises the branch ``if entity_type:`` + ``if user_id is not None:``
    (lines :117 pre-fix) so both user_id branches of get_knowledge_graph are covered.
    """
    from paper_ingestion.extraction.entities_sql import get_knowledge_graph

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('f15-typed-entity', 'f15-typed-entity', 'method', 0)
           RETURNING id"""
    )

    # A: 1 paper_entities row.
    paper_a = await _seed_paper(contract_conn, "f15-typed-paper-a", user_a_id)
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, mention_count, user_id)
           VALUES ($1, $2, 1, $3)
           ON CONFLICT (paper_id, entity_id, user_id) DO NOTHING""",
        paper_a,
        entity_id,
        user_a_id,
    )

    # B: 2 paper_entities rows.
    paper_b1 = await _seed_paper(contract_conn, "f15-typed-paper-b1", user_b_id)
    paper_b2 = await _seed_paper(contract_conn, "f15-typed-paper-b2", user_b_id)
    for paper_id in (paper_b1, paper_b2):
        await contract_conn.execute(
            """INSERT INTO paper_entities (paper_id, entity_id, mention_count, user_id)
               VALUES ($1, $2, 1, $3)
               ON CONFLICT (paper_id, entity_id, user_id) DO NOTHING""",
            paper_id,
            entity_id,
            user_b_id,
        )

    # global paper_count = 3.
    await contract_conn.execute(
        "UPDATE entities SET paper_count = 3 WHERE id = $1",
        entity_id,
    )

    result_a = await get_knowledge_graph(
        contract_conn, entity_type="method", min_paper_count=2, user_id=user_a_id
    )
    result_b = await get_knowledge_graph(
        contract_conn, entity_type="method", min_paper_count=2, user_id=user_b_id
    )

    names_a = {e["name"] for e in result_a["entities"]}
    names_b = {e["name"] for e in result_b["entities"]}

    assert "f15-typed-entity" not in names_a, (
        "BUG: user A (per-user count=1) must not see typed entity at min_paper_count=2; "
        f"names_a={names_a}"
    )
    assert "f15-typed-entity" in names_b, (
        f"User B (per-user count=2) must see typed entity at min_paper_count=2; names_b={names_b}"
    )
