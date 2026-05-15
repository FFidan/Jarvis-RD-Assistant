"""Cross-user scoping for entity_relationships reads (close cross-user leak).

Entities are scoped per-user via ``paper_entities.user_id``. The
``entity_relationships`` table, however, has a ``paper_id`` column but NO
user scoping — a relationship whose ``paper_id`` belongs to ANOTHER user's
explicitly-owned paper was previously returned to the caller whenever both
endpoint entities happened to be visible.

These tests verify the canonical-corpus visibility predicate (mirrors
``list_contradictions``: ``user_library`` EXISTS + ``discovered_by IS NULL``
free pass) is applied to every ``entity_relationships`` read path:

* ``get_knowledge_graph`` (helper)
* ``query_knowledge_graph`` (helper, scoped branches)
* ``get_entity_detail`` (router endpoint)

Rule (decided, canonical corpus):
  * ``papers.discovered_by IS NULL``       → shared/system → visible to all.
  * ``discovered_by = caller``             → caller's own  → visible.
  * row in ``user_library(caller, paper)`` → in library    → visible.
  * else (another user's explicitly-owned, non-library)    → NOT visible.

Mirrors the cross-user fixture style of ``test_contradictions_scoping.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake connection that actually evaluates the visibility rule.
#
# Instead of asserting on SQL strings only, this fake models the relevant
# slice of the schema (papers.discovered_by, user_library, a set of
# entity_relationships rows) and applies the *decided* visibility rule to
# whatever the production SQL asks for. If production drops the predicate the
# fake still returns everything → the leak assertion fails. This makes the
# test a behavioural guard, not a string-match guard.
# ---------------------------------------------------------------------------


# user A = 1, user B = 2.
# Papers:
#   100 → discovered_by = 1 (user A's explicitly-owned, NOT in B's library)
#   200 → discovered_by NULL (shared / system / Pulse-discovered)
#   300 → discovered_by = 1 but ALSO in user B's user_library
_PAPERS = {100: 1, 200: None, 300: 1}
_USER_LIBRARY = {(2, 300)}  # (user_id, paper_id)

# Relationships: all between the same visible entity pair (1, 2).
_RELATIONSHIPS = [
    {
        "id": 1,
        "source_entity_id": 1,
        "target_entity_id": 2,
        "relationship_type": "evaluates",
        "paper_id": 100,  # user A's owned paper → must be HIDDEN from B
        "evidence_quote": "leak",
        "confidence": 0.9,
        "metadata": {},
        "created_at": None,
    },
    {
        "id": 2,
        "source_entity_id": 1,
        "target_entity_id": 2,
        "relationship_type": "uses",
        "paper_id": 200,  # shared canonical paper → must be VISIBLE to B
        "evidence_quote": "shared",
        "confidence": 0.8,
        "metadata": {},
        "created_at": None,
    },
    {
        "id": 3,
        "source_entity_id": 1,
        "target_entity_id": 2,
        "relationship_type": "compares",
        "paper_id": 300,  # A-owned but in B's library → VISIBLE to B
        "evidence_quote": "in-library",
        "confidence": 0.7,
        "metadata": {},
        "created_at": None,
    },
]


def _paper_visible_to(paper_id: int | None, user_id: int) -> bool:
    """The decided canonical-corpus visibility rule."""
    if paper_id is None:
        return True  # unattributable (ON DELETE SET NULL) → visible
    discovered_by = _PAPERS.get(paper_id)
    if discovered_by is None:
        return True  # shared / system
    if discovered_by == user_id:
        return True  # caller's own
    return (user_id, paper_id) in _USER_LIBRARY  # explicit library membership


class _ScopingFakeConn:
    """Emulates the entity_relationships read honoring the SQL predicate.

    Only the ``entity_relationships`` SELECTs are interpreted; the entity
    SELECT(s) are answered with the fixed visible-entity set so the helper
    proceeds to the relationship fetch under test.
    """

    def __init__(self, *, user_id: int) -> None:
        self._user_id = user_id
        self.relationship_sql: str | None = None

    async def fetch(self, sql: str, *args):  # noqa: ANN002
        norm = " ".join(sql.split())
        if "FROM entities" in norm and "entity_relationships" not in norm:
            # Entity node fetch → both endpoint entities are visible to B.
            return [
                {
                    "id": 1,
                    "name": "BERT",
                    "canonical_name": "bert",
                    "entity_type": "method",
                    "description": None,
                    "metadata": {},
                    "embedding_id": None,
                    "paper_count": 2,
                    "created_at": None,
                },
                {
                    "id": 2,
                    "name": "GLUE",
                    "canonical_name": "glue",
                    "entity_type": "dataset",
                    "description": None,
                    "metadata": {},
                    "embedding_id": None,
                    "paper_count": 2,
                    "created_at": None,
                },
            ]
        if "entity_relationships" in norm:
            self.relationship_sql = norm
            scoped = "user_library" in norm and "discovered_by" in norm and self._user_id in args
            out = []
            for rel in _RELATIONSHIPS:
                if scoped and not _paper_visible_to(rel["paper_id"], self._user_id):
                    continue
                out.append(dict(rel))
            return out
        return []


# ---------------------------------------------------------------------------
# get_knowledge_graph helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_knowledge_graph_hides_other_users_relationship():
    """User B's KG must NOT include an edge from user A's owned paper, but
    MUST include edges from a shared (discovered_by NULL) paper and from an
    A-owned paper that is in B's library."""
    from paper_ingestion.extraction.entities import get_knowledge_graph

    conn = _ScopingFakeConn(user_id=2)
    result = await get_knowledge_graph(conn, user_id=2)

    rel_ids = sorted(r["id"] for r in result["relationships"])
    paper_ids = sorted(r["paper_id"] for r in result["relationships"])

    # Edge id=1 (paper 100, A-owned, not in B's library) must be hidden.
    assert 1 not in rel_ids, f"cross-user leak: edge from A-owned paper leaked: {rel_ids}"
    # Edge id=2 (paper 200, shared canonical) must be visible.
    assert 2 in rel_ids, "shared canonical edge wrongly hidden"
    # Edge id=3 (paper 300, A-owned but in B's library) must be visible.
    assert 3 in rel_ids, "in-library edge wrongly hidden"
    assert paper_ids == [200, 300]

    # The scoped SQL must carry the canonical-visibility predicate.
    assert conn.relationship_sql is not None
    assert "user_library" in conn.relationship_sql
    assert "discovered_by" in conn.relationship_sql


@pytest.mark.asyncio
async def test_get_knowledge_graph_unscoped_path_unchanged():
    """user_id=None (server-to-server) preserves legacy unscoped behavior."""
    from paper_ingestion.extraction.entities import get_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.side_effect = [
        [{"id": 1, "name": "BERT", "entity_type": "method", "paper_count": 1}],
        [
            {
                "id": 1,
                "source_entity_id": 1,
                "target_entity_id": 1,
                "relationship_type": "self",
                "paper_id": 100,
                "confidence": 1.0,
            }
        ],
    ]

    result = await get_knowledge_graph(mock_conn)

    assert len(result["relationships"]) == 1
    rel_sql = " ".join(mock_conn.fetch.call_args_list[1].args[0].split())
    assert "user_library" not in rel_sql, "unscoped path must not add the predicate"


# ---------------------------------------------------------------------------
# query_knowledge_graph helper (scoped branches)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "marker"),
    [
        ("What methods are used on GLUE?", "used_on"),
        ("What outperforms BM25?", "outperforms"),
    ],
)
async def test_query_knowledge_graph_scoped_branches_carry_predicate(query, marker):
    """Both relationship-driven query branches must thread user_id into a
    canonical-visibility predicate over entity_relationships.paper_id."""
    from paper_ingestion.extraction.entities import query_knowledge_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    await query_knowledge_graph(mock_conn, query, user_id=2)

    sql = " ".join(mock_conn.fetch.call_args.args[0].split())
    params = mock_conn.fetch.call_args.args[1:]
    assert "entity_relationships" in sql
    assert "user_library" in sql, f"{marker}: missing user_library predicate"
    assert "discovered_by" in sql, f"{marker}: missing discovered_by predicate"
    assert 2 in params, f"{marker}: user_id not threaded into SQL params"


# ---------------------------------------------------------------------------
# get_entity_detail router endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_detail_relationships_scoped_to_caller(monkeypatch):
    """get_entity_detail must scope its entity_relationships fetch to the
    caller's visible papers; the unscoped (user_id=None) branch is unchanged.
    """
    import paper_ingestion.routers.knowledge_graph as kg_router

    monkeypatch.setattr(
        kg_router,
        "current_user_id_strict",
        AsyncMock(return_value=2),  # user B
    )

    conn = AsyncMock()
    # entity lookup, visibility check, relationship fetch, papers fetch.
    conn.fetchrow.return_value = {
        "id": 1,
        "name": "BERT",
        "canonical_name": "bert",
        "entity_type": "method",
        "description": None,
        "metadata": {},
        "paper_count": 1,
        "created_at": None,
    }
    conn.fetchval.return_value = 1  # entity visible to user B
    conn.fetch.side_effect = [
        # entity_relationships fetch — DB already applied predicate, returns
        # only the shared-canonical edge (paper 200).
        [
            {
                "id": 2,
                "source_entity_id": 1,
                "target_entity_id": 2,
                "relationship_type": "uses",
                "paper_id": 200,
                "evidence_quote": "shared",
                "confidence": 0.8,
                "created_at": None,
            }
        ],
        # papers-mentioning-entity fetch.
        [],
    ]

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    result = await kg_router.get_entity_detail.__wrapped__(
        MagicMock(),
        entity_id=1,
        db_pool=pool,
    )

    assert [r.id for r in result.relationships] == [2]
    # The relationship fetch (first conn.fetch call) must be the scoped SQL.
    rel_sql = " ".join(conn.fetch.call_args_list[0].args[0].split())
    rel_params = conn.fetch.call_args_list[0].args[1:]
    assert "entity_relationships" in rel_sql
    assert "user_library" in rel_sql, "get_entity_detail relationship read not scoped"
    assert "discovered_by" in rel_sql
    assert 2 in rel_params, "caller user_id not threaded into the predicate"
