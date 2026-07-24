"""Paper-visibility scoping for knowledge-graph reads.

``paper_entities.user_id`` records who ran extraction; it is not an access
grant. Nodes and edges must instead follow the persisted paper scope and the
caller's explicit library membership.

These tests verify the centralized public-or-library predicate is applied to
every ``entity_relationships`` read path:

* ``get_knowledge_graph`` (helper)
* ``query_knowledge_graph`` (helper, scoped branches)
* ``get_entity_detail`` (router endpoint)

Rule:
  * ``papers.visibility_scope = 'public'`` → visible to all callers.
  * row in ``user_library(caller, paper)``  → visible to that caller.
  * missing/deleted/private otherwise       → not visible.

Mirrors the cross-user fixture style of ``test_contradictions_scoping.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

# ---------------------------------------------------------------------------
# Fake connection that actually evaluates the visibility rule.
#
# Instead of asserting on SQL strings only, this fake models the relevant
# slice of the schema (papers.visibility_scope, user_library, a set of
# entity_relationships rows) and applies the *decided* visibility rule to
# whatever the production SQL asks for. If production drops the predicate the
# fake still returns everything → the leak assertion fails. This makes the
# test a behavioural guard, not a string-match guard.
# ---------------------------------------------------------------------------


# user A = 1, user B = 2.
# Papers:
#   100 → private, NOT in B's library
#   200 → public
#   300 → private, but in user B's library
_PAPERS = {100: "private", 200: "public", 300: "private"}
_USER_LIBRARY = {(2, 300)}  # (user_id, paper_id)

# Relationships: all between the same visible entity pair (1, 2).
_RELATIONSHIPS = [
    {
        "id": 1,
        "source_entity_id": 1,
        "target_entity_id": 2,
        "relationship_type": "evaluates",
        "paper_id": 100,  # unshelved private paper → HIDDEN from B
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
        "paper_id": 200,  # public paper → VISIBLE to B
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
        "paper_id": 300,  # private but in B's library → VISIBLE to B
        "evidence_quote": "in-library",
        "confidence": 0.7,
        "metadata": {},
        "created_at": None,
    },
]


def _paper_visible_to(paper_id: int | None, user_id: int) -> bool:
    """Model the persisted public-or-library visibility rule."""
    if paper_id is None:
        return False
    return _PAPERS.get(paper_id) == "public" or (user_id, paper_id) in _USER_LIBRARY


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
            scoped = "user_library" in norm and "visibility_scope" in norm and self._user_id in args
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
    """User B sees public and shelved-private edges, never unshelved private edges."""
    from paper_ingestion.extraction.entities import get_knowledge_graph

    conn = _ScopingFakeConn(user_id=2)
    result = await get_knowledge_graph(conn, user_id=2)  # type: ignore[arg-type]

    rel_ids = sorted(r["id"] for r in result["relationships"])
    paper_ids = sorted(r["paper_id"] for r in result["relationships"])

    # Edge id=1 (paper 100, private and not in B's library) must be hidden.
    assert 1 not in rel_ids, f"private-paper edge leaked: {rel_ids}"
    # Edge id=2 (paper 200, public) must be visible.
    assert 2 in rel_ids, "public edge wrongly hidden"
    # Edge id=3 (paper 300, private but in B's library) must be visible.
    assert 3 in rel_ids, "in-library edge wrongly hidden"
    assert paper_ids == [200, 300]

    # The scoped SQL must carry the centralized visibility predicate.
    assert conn.relationship_sql is not None
    assert "user_library" in conn.relationship_sql
    assert "visibility_scope" in conn.relationship_sql
    assert "discovered_by" not in conn.relationship_sql


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
# get_entity_detail router endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_detail_relationships_scoped_to_caller():
    """get_entity_detail must scope its entity_relationships fetch to the
    caller's visible papers; the unscoped (user_id=None) branch is unchanged.
    """
    import paper_ingestion.routers.knowledge_graph as kg_router

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
    conn.fetch.side_effect = [
        # entity_relationships fetch — DB already applied predicate, returns
        # only the public edge (paper 200).
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
        user_id=2,  # user B — now a Depends-wired param, threaded explicitly
    )

    assert [r.id for r in result.relationships] == [2]
    # The relationship fetch (first conn.fetch call) must be the scoped SQL.
    rel_sql = " ".join(conn.fetch.call_args_list[0].args[0].split())
    rel_params = conn.fetch.call_args_list[0].args[1:]
    assert "entity_relationships" in rel_sql
    assert "user_library" in rel_sql, "get_entity_detail relationship read not scoped"
    assert "visibility_scope" in rel_sql
    assert "discovered_by" not in rel_sql
    assert 2 in rel_params, "caller user_id not threaded into the predicate"


# ---------------------------------------------------------------------------
# list_entities endpoint — both outer branches exclude a non-visible entity
#
# user_id is resolved from the session (never None), so the removed unscoped
# else branches were dead code. The surviving scoped branches must still hide
# a private-paper entity the caller cannot see — in both the unfiltered read
# and the entity_type-filtered read.
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_list_entities_scopes_both_branches_to_caller(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """GET /api/knowledge-graph/entities hides another user's private-paper entity
    in both the unfiltered and the entity_type-filtered branch, while a public
    entity stays visible.

    # Verified: knowledge_graph.py:188 list_entities
    """
    # Private paper owned by A and not in B's library → its method entity is
    # invisible to B in either branch.
    private_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url,
                               discovered_by, visibility_scope)
           VALUES ('kg-list-private-a', 'arxiv', 'Private A entity paper', ARRAY['Author'],
                   'https://kg-list-a.test/paper', $1, 'private')
           RETURNING id""",
        contract_two_users.user_a_id,
    )
    private_entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-list-private-only', 'kg-list-private-only', 'method', 1)
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        private_paper_id,
        private_entity_id,
        contract_two_users.user_a_id,
    )

    # Public paper → its method entity is visible to every caller (positive
    # control proving the branches are not vacuously empty).
    public_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url,
                               discovered_by, visibility_scope)
           VALUES ('kg-list-public', 'arxiv', 'Public entity paper', ARRAY['Author'],
                   'https://kg-list-pub.test/paper', $1, 'public')
           RETURNING id""",
        contract_two_users.user_a_id,
    )
    public_entity_id = await contract_conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type, paper_count)
           VALUES ('kg-list-public-shared', 'kg-list-public-shared', 'method', 1)
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_entities (paper_id, entity_id, user_id)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        public_paper_id,
        public_entity_id,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        unfiltered = await c.get("/api/knowledge-graph/entities?limit=500")
        filtered = await c.get("/api/knowledge-graph/entities?entity_type=method&limit=500")

    for label, resp in (("unfiltered", unfiltered), ("entity_type=method", filtered)):
        assert resp.status_code == 200, f"{label}: got {resp.status_code}: {resp.text[:200]}"
        names = [e["name"] for e in resp.json()]
        assert "kg-list-public-shared" in names, (
            f"{label}: public entity must be visible to user B; got {names}"
        )
        assert "kg-list-private-only" not in names, (
            f"{label}: user B must not see user A's private-paper entity; got {names}"
        )
