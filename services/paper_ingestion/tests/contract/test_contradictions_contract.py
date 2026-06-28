"""Contradictions router contract tests — Cluster 5.

Covers GET /api/contradictions, POST /api/contradictions/scan, and
POST /api/papers/{paper_id}/contradictions/scan. Replaces mock-unit tests in
services/paper_ingestion/tests/test_contradictions_router.py (3 tests) and the
service-router-shaped mock-units in test_contradictions_service.py with
survivor citations.

  test_get_contradictions_returns_verified_rows
  test_list_contradictions_maps_rows
  test_scan_contradictions_endpoint_enqueues_job
  test_scan_paper_contradictions_endpoint_enqueues_scoped_job
  test_pair_construction_uses_cross_ref_index_for_library_scan
  test_pair_construction_full_scan_when_paper_id_provided
  test_persist_contradiction_dedup_uses_direct_equality

Carve-out:
  - task_registry._TASK_MAP for contradictions.scan
  - AsyncOpenAI / call_llm_structured: NOT touched by these endpoints —
    they are invoked only inside the deferred task body, not in the HTTP path.
    Service-level tests (test_contradictions_service.py) that mock AsyncOpenAI
    remain as boundary-adapter survivors.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_contradiction(
    conn, paper_a_id: int, paper_b_id: int, user_id: int, *, status: str = "verified"
) -> int:
    """Insert a paper_contradictions row owned by *user_id*."""
    return int(
        await conn.fetchval(
            """
            INSERT INTO paper_contradictions (
                paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
                contradiction_type, explanation, confidence, status, user_id
            ) VALUES ($1, $2, 'A says X', 'B says not X', 'quote A',
                      'quote B', 'direct', 'they disagree', 0.85, $3, $4)
            RETURNING id
            """,
            paper_a_id,
            paper_b_id,
            status,
            user_id,
        )
    )


async def _seed_stance_row(
    conn,
    paper_a_id: int,
    paper_b_id: int,
    user_id: int,
    *,
    stance: str,
    claim_topic: str,
    quote_a: str = "quote A",
    quote_b: str = "quote B",
) -> int:
    """Insert a paper_contradictions row carrying a stance + claim_topic."""
    return int(
        await conn.fetchval(
            """
            INSERT INTO paper_contradictions (
                paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
                contradiction_type, explanation, confidence, status, user_id,
                stance, claim_topic
            ) VALUES ($1, $2, 'A says', 'B says', $3, $4, 'direct',
                      'they relate', 0.85, 'verified', $5, $6, $7)
            RETURNING id
            """,
            paper_a_id,
            paper_b_id,
            quote_a,
            quote_b,
            user_id,
            stance,
            claim_topic,
        )
    )


async def _seed_library_paper(conn, user_id: int, external_id: str) -> int:
    """Insert a paper owned by *user_id* and add it to their library."""
    paper_id = int(
        await conn.fetchval(
            """
            INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
            VALUES ($1, 'arxiv', 'Paper', ARRAY['A'], $2, $3)
            RETURNING id
            """,
            external_id,
            f"https://example.test/{external_id}",
            user_id,
        )
    )
    await conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_id,
        paper_id,
    )
    return paper_id


# ---------------------------------------------------------------------------
# GET /api/contradictions — list scoped to caller
# ---------------------------------------------------------------------------


async def test_c5_01_list_contradictions_returns_rows_scoped_to_user(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/contradictions returns the caller's rows; not others' (IDOR).

    # Verified: services/paper_ingestion/paper_ingestion/routers/contradictions.py:29
    # (get_contradictions calls list_contradictions(conn, user_id=user_id, ...)).
    """
    paper_b_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('contra-paper-b-a', 'arxiv', 'Paper B (A)', ARRAY['B'],
                'https://example.test/b', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    cid_a = await _seed_contradiction(
        contract_conn, contract_two_users.paper_id_a, paper_b_id, contract_two_users.user_a_id
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/contradictions")
    assert resp_a.status_code == 200, resp_a.text[:300]
    body_a = resp_a.json()
    ids_a = [r["id"] for r in body_a["contradictions"]]
    assert cid_a in ids_a, f"User A should see own contradiction {cid_a}; got {ids_a}"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/contradictions")
    assert resp_b.status_code == 200, resp_b.text[:300]
    ids_b = [r["id"] for r in resp_b.json()["contradictions"]]
    assert cid_a not in ids_b, f"IDOR leak: user B saw user A's contradiction {cid_a}: {ids_b}"


# ---------------------------------------------------------------------------
# GET /api/contradictions — user B sees empty when A seeded rows
# (sub-assertion of the list-scoped-to-caller test — kept separate for clarity)
# ---------------------------------------------------------------------------


async def test_c5_02_list_contradictions_user_b_returns_empty(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """User B with no seeded contradictions returns total=0.

    # Verified: services/paper_ingestion/paper_ingestion/routers/contradictions.py:29
    # (response.total === len(rows) when user has no contradictions).
    """
    paper_b_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('contra-paper-c5-02', 'arxiv', 'Paper B', ARRAY['B'],
                'https://example.test/b', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    await _seed_contradiction(
        contract_conn, contract_two_users.paper_id_a, paper_b_id, contract_two_users.user_a_id
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/contradictions")
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body.get("contradictions") == [], (
        f"User B should see no contradictions; got {body['contradictions']}"
    )
    assert body.get("total") == 0


# ---------------------------------------------------------------------------
# POST /api/contradictions/scan — enqueues 202 + job_id
# ---------------------------------------------------------------------------


async def test_c5_03_scan_enqueues_202_with_job_id(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/contradictions/scan returns 202 + job_id; task_registry carve-out.

    # Verified: services/paper_ingestion/paper_ingestion/routers/contradictions.py:51
    # (scan_contradictions defers contradictions.scan via KIND_TO_TASK).
    """
    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"contradictions.scan": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/contradictions/scan", json={})

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("job_id"), f"Missing job_id: {body}"
    assert body.get("status") == "queued"
    mock_task.defer_async.assert_awaited_once()
    call_kwargs = mock_task.defer_async.call_args.kwargs
    assert str(call_kwargs["user_id"]) == str(contract_two_users.user_a_id)


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/contradictions/scan — ownership-scoped
# ---------------------------------------------------------------------------


async def test_c5_04_scan_paper_scoped_enqueues_with_ownership_check(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/papers/{id}/contradictions/scan: owner enqueues; non-owner 403/404.

    # Verified: services/paper_ingestion/paper_ingestion/routers/contradictions.py:68
    # (scan_paper_contradictions calls assert_paper_ownership before defer_async).
    """
    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"contradictions.scan": mock_task}):
        # User A (owner) can enqueue
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp_a = await c.post(
                f"/api/papers/{contract_two_users.paper_id_a}/contradictions/scan",
                json={"limit": 5},
            )
        assert resp_a.status_code == 202, resp_a.text[:300]
        mock_task.defer_async.assert_awaited()

        # Reset defer_async tracking before user B attempt
        mock_task.defer_async.reset_mock()

        # User B (non-owner) gets 403/404
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
            resp_b = await c.post(
                f"/api/papers/{contract_two_users.paper_id_a}/contradictions/scan",
                json={"limit": 5},
            )
        assert resp_b.status_code in (403, 404), (
            f"Non-owner should get 403/404; got {resp_b.status_code}: {resp_b.text[:300]}"
        )
        mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /api/contradictions?paper_id=X — filter by paper
# ---------------------------------------------------------------------------


async def test_c5_05_list_contradictions_paper_id_filter(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/contradictions?paper_id=X returns only rows matching paper_a_id or paper_b_id=X.

    # Verified: services/paper_ingestion/paper_ingestion/routers/contradictions.py:29
    # (paper_id query param is passed into list_contradictions filter).
    """
    paper_x_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('contra-paper-x', 'arxiv', 'Paper X', ARRAY['X'],
                'https://example.test/x', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    paper_y_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('contra-paper-y', 'arxiv', 'Paper Y', ARRAY['Y'],
                'https://example.test/y', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    paper_z_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('contra-paper-z', 'arxiv', 'Paper Z', ARRAY['Z'],
                'https://example.test/z', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )

    # list_contradictions scopes via EXISTS user_library: at least one paper in
    # each contradiction pair must be in the caller's library. Add paper_y_id
    # (which appears in BOTH contradictions below) to user A's library.
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_y_id,
    )

    cid_xy = await _seed_contradiction(
        contract_conn, paper_x_id, paper_y_id, contract_two_users.user_a_id
    )
    cid_yz = await _seed_contradiction(
        contract_conn, paper_y_id, paper_z_id, contract_two_users.user_a_id
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/contradictions?paper_id={paper_x_id}")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    ids = [r["id"] for r in body["contradictions"]]
    assert cid_xy in ids, f"Paper X's contradiction {cid_xy} missing from filtered list: {ids}"
    assert cid_yz not in ids, (
        f"paper_id={paper_x_id} filter leaked unrelated contradiction {cid_yz}: {ids}"
    )


# ---------------------------------------------------------------------------
# _persist_contradiction writes stance + claim_topic against the live schema
# ---------------------------------------------------------------------------


async def test_c5_06_persist_writes_stance_and_claim_topic(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """A 'supports' assessment persists stance + claim_topic and reads back.

    # Verified: contradictions_persist.py _persist_contradiction INSERT column
    # list includes stance + claim_topic (migration 0098). Exercises the real
    # INSERT against the migrated schema, not a mock.
    """
    from paper_ingestion.services.contradiction_models import ContradictionClassification
    from paper_ingestion.services.contradictions_extract import (
        ContradictionCandidate,
        VerifiedFinding,
    )
    from paper_ingestion.services.contradictions_persist import _persist_contradiction

    paper_a_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('contra-supp-a', 'arxiv', 'Paper A', ARRAY['A'],
                'https://example.test/sa', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    paper_b_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('contra-supp-b', 'arxiv', 'Paper B', ARRAY['B'],
                'https://example.test/sb', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )

    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=paper_a_id,
            title="Paper A",
            finding="A finds X",
            quote="Paper A supports X.",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        b=VerifiedFinding(
            paper_id=paper_b_id,
            title="Paper B",
            finding="B finds X",
            quote="Paper B also supports X.",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        score=0.9,
        reason="cross_reference",
    )
    classification = ContradictionClassification(
        is_contradiction=False,
        stance="supports",
        claim_topic="whether X holds",
        explanation="Both findings affirm X.",
        quote_a="Paper A supports X.",
        quote_b="Paper B also supports X.",
        confidence=0.8,
    )

    cid = await _persist_contradiction(
        contract_conn,
        candidate,
        classification,
        page_a=1,
        page_b=2,
        model="test-model",
        user_id=contract_two_users.user_a_id,
    )
    assert cid is not None

    row = await contract_conn.fetchrow(
        "SELECT stance, claim_topic FROM paper_contradictions WHERE id = $1", cid
    )
    assert row["stance"] == "supports"
    assert row["claim_topic"] == "whether X holds"


# ---------------------------------------------------------------------------
# GET /api/consensus — aggregate supports/opposes per shared claim
# ---------------------------------------------------------------------------


async def test_c5_07_consensus_aggregates_by_normalized_claim(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/consensus folds near-duplicate claim topics and counts stances.

    # Verified: contradictions_persist.aggregate_consensus groups on a
    # normalized claim_topic (lowercased, punctuation collapsed).
    """
    user_a = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_a, "consensus-p1")
    p2 = await _seed_library_paper(contract_conn, user_a, "consensus-p2")
    p3 = await _seed_library_paper(contract_conn, user_a, "consensus-p3")

    await _seed_stance_row(
        contract_conn, p1, p2, user_a, stance="supports", claim_topic="effect of X on Y"
    )
    # Near-duplicate phrasing must fold into the same cluster.
    await _seed_stance_row(
        contract_conn, p1, p3, user_a, stance="supports", claim_topic="Effect of X, on Y"
    )
    await _seed_stance_row(
        contract_conn, p2, p3, user_a, stance="opposes", claim_topic="effect of x on y"
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/consensus")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["total"] == 1, f"near-duplicate topics must fold to one cluster: {body}"
    claim = body["claims"][0]
    assert claim["supports"] == 2, f"expected 2 supports: {claim}"
    assert claim["opposes"] == 1, f"expected 1 opposes: {claim}"
    assert sorted(claim["paper_ids"]) == sorted([p1, p2, p3])
    # Each cluster carries its verified evidence (quotes + pages) for drill-down.
    assert len(claim["assessments"]) == 3, f"expected 3 assessments: {claim}"
    assert all(a["quote_a"] and a["quote_b"] for a in claim["assessments"])
    assert {a["stance"] for a in claim["assessments"]} == {"supports", "opposes"}


async def test_c5_08_supports_excluded_from_contradictions_list(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """A persisted 'supports' row must not appear in GET /api/contradictions.

    # Verified: list_contradictions adds a stance predicate
    # (pc.stance IS NULL OR pc.stance = 'opposes').
    """
    user_a = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_a, "stance-filter-p1")
    p2 = await _seed_library_paper(contract_conn, user_a, "stance-filter-p2")

    supports_id = await _seed_stance_row(
        contract_conn, p1, p2, user_a, stance="supports", claim_topic="topic"
    )
    opposes_id = await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_a,
        stance="opposes",
        claim_topic="topic",
        quote_a="qx",
        quote_b="qy",
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/contradictions")

    assert resp.status_code == 200, resp.text[:300]
    ids = [r["id"] for r in resp.json()["contradictions"]]
    assert opposes_id in ids, f"opposes row should remain a contradiction: {ids}"
    assert supports_id not in ids, f"supports row leaked into contradiction list: {ids}"


async def test_c5_09_consensus_tenancy_isolation(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """User B cannot see a consensus cluster built only from user A's papers.

    # Verified: aggregate_consensus reuses the user_library OR-predicate.
    """
    user_a = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_a, "consensus-tenancy-p1")
    p2 = await _seed_library_paper(contract_conn, user_a, "consensus-tenancy-p2")
    await _seed_stance_row(
        contract_conn, p1, p2, user_a, stance="supports", claim_topic="private claim"
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/consensus")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["total"] == 0, f"user B must not see user A's private cluster: {body}"
    assert body["claims"] == []


async def test_c5_10_unique_index_ignores_stance_and_topic_labels(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """The unique index keys on pair + quotes only; a drifted label still collides.

    # Verified: migration 0098 leaves idx_paper_contradictions_unique_quotes on
    # (pair, md5 quotes), so a re-scan of the same evidence with a drifted
    # stance/claim_topic label hits the same key and is deduped (the persist
    # UniqueViolation -> reuse fallback returns the existing row in production,
    # where each statement autocommits). Were the label part of the key, the
    # near-duplicate would persist and double-count in the consensus aggregation.
    """
    user_a = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_a, "dedup-p1")
    p2 = await _seed_library_paper(contract_conn, user_a, "dedup-p2")

    await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_a,
        stance="opposes",
        claim_topic="effect of X on Y",
        quote_a="Paper A says X.",
        quote_b="Paper B says not X.",
    )
    # Same pair + identical verbatim quotes, drifted stance + claim_topic label.
    with pytest.raises(asyncpg.UniqueViolationError):
        await _seed_stance_row(
            contract_conn,
            p1,
            p2,
            user_a,
            stance="supports",
            claim_topic="Effect of X, on Y!",
            quote_a="Paper A says X.",
            quote_b="Paper B says not X.",
        )
