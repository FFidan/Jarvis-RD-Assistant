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

from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.contradictions import (
    ContradictionCandidate,
    VerifiedFinding,
    _persist_contradiction,
)
from paper_ingestion.services.contradictions_extract import _load_verified_findings

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


async def _seed_summary_findings(
    conn,
    paper_id: int,
    *,
    user_id: int | None,
    finding: str,
    quote: str,
    related_paper_id: int | None = None,
    related_content_generation: int = 0,
) -> None:
    """Insert a paper_summaries row with one verified-style key finding."""
    refs = (
        []
        if related_paper_id is None
        else [
            {
                "related_paper_id": related_paper_id,
                "content_generation": related_content_generation,
            }
        ]
    )
    await conn.execute(
        """
        INSERT INTO paper_summaries (
            paper_id, user_id, summary_brief, summary_detailed, key_findings, cross_references
        )
        VALUES ($1, $2, 'brief', 'detailed', $3::jsonb, $4::jsonb)
        """,
        paper_id,
        user_id,
        [{"finding": finding, "quote": quote, "page_number": 1}],
        refs,
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
# Finding load scoping
# ---------------------------------------------------------------------------


async def test_load_verified_findings_requires_user_library_membership(
    contract_two_users, contract_conn
):
    """Library scans load only findings for papers in the caller's library."""
    user_a = contract_two_users.user_a_id
    owned = await _seed_library_paper(contract_conn, user_a, "finding-owned")
    unowned = await _seed_library_paper(
        contract_conn, contract_two_users.user_b_id, "finding-unowned"
    )
    await _seed_summary_findings(
        contract_conn, owned, user_id=None, finding="owned finding", quote="owned quote"
    )
    await _seed_summary_findings(
        contract_conn, unowned, user_id=None, finding="unowned finding", quote="unowned quote"
    )

    findings = await _load_verified_findings(contract_conn, user_id=user_a)

    assert {finding.paper_id for finding in findings} == {owned}


async def test_load_verified_findings_focused_scan_keeps_related_papers_in_library(
    contract_two_users, contract_conn
):
    """Focused scans do not pull unowned related papers through cross-references."""
    user_a = contract_two_users.user_a_id
    owned = await _seed_library_paper(contract_conn, user_a, "focused-owned")
    unowned = await _seed_library_paper(
        contract_conn, contract_two_users.user_b_id, "focused-unowned"
    )
    await _seed_summary_findings(
        contract_conn,
        owned,
        user_id=None,
        finding="owned focused finding",
        quote="owned focused quote",
        related_paper_id=unowned,
    )
    await _seed_summary_findings(
        contract_conn,
        unowned,
        user_id=None,
        finding="unowned related finding",
        quote="unowned related quote",
    )

    findings = await _load_verified_findings(contract_conn, paper_id=owned, user_id=user_a)

    assert {finding.paper_id for finding in findings} == {owned}


async def test_cross_reference_generation_filters_only_the_stale_target(
    contract_two_users, contract_conn
):
    """Advancing a related paper removes its link but keeps the source findings."""
    user_id = contract_two_users.user_a_id
    source = await _seed_library_paper(contract_conn, user_id, "cross-gen-source")
    target = await _seed_library_paper(contract_conn, user_id, "cross-gen-target")
    await _seed_summary_findings(
        contract_conn,
        source,
        user_id=user_id,
        finding="source finding",
        quote="source quote",
        related_paper_id=target,
        related_content_generation=0,
    )

    before = await _load_verified_findings(
        contract_conn,
        paper_id=target,
        user_id=user_id,
    )
    assert [finding.paper_id for finding in before] == [source]
    assert before[0].cross_reference_ids == frozenset({target})

    await contract_conn.execute(
        "UPDATE papers SET content_generation = 1 WHERE id = $1",
        target,
    )

    focused_target = await _load_verified_findings(
        contract_conn,
        paper_id=target,
        user_id=user_id,
    )
    focused_source = await _load_verified_findings(
        contract_conn,
        paper_id=source,
        user_id=user_id,
    )
    assert focused_target == []
    assert [finding.finding for finding in focused_source] == ["source finding"]
    assert focused_source[0].cross_reference_ids == frozenset()


# ---------------------------------------------------------------------------
# GET /api/contradictions — list scoped to caller
# ---------------------------------------------------------------------------


async def test_c5_01_list_contradictions_returns_rows_scoped_to_user(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/contradictions returns the caller's rows; not others' (IDOR).

    Both evidence papers sit in BOTH libraries, so the library predicate admits
    user B for every row. Only row ownership can exclude A's assessment, which
    is the property under test: sharing the papers must not share the reading.

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
    for holder in (contract_two_users.user_a_id, contract_two_users.user_b_id):
        for shared_paper_id in (contract_two_users.paper_id_a, paper_b_id):
            await contract_conn.execute(
                """
                INSERT INTO user_library (user_id, paper_id, added_via)
                VALUES ($1, $2, 'manual_save')
                ON CONFLICT (user_id, paper_id) DO NOTHING
                """,
                holder,
                shared_paper_id,
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


async def test_c5_01b_list_contradictions_hides_mixed_library_pair(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """A row owned by the caller is hidden when either evidence paper is outside their library."""
    user_a = contract_two_users.user_a_id
    owned = await _seed_library_paper(contract_conn, user_a, "mixed-list-owned")
    unowned = await _seed_library_paper(
        contract_conn, contract_two_users.user_b_id, "mixed-list-unowned"
    )
    mixed_id = await _seed_contradiction(contract_conn, owned, unowned, user_a)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/contradictions")

    assert resp.status_code == 200, resp.text[:300]
    ids = [row["id"] for row in resp.json()["contradictions"]]
    assert mixed_id not in ids


async def test_c5_02_list_contradictions_user_b_returns_empty(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """User B with no seeded contradictions returns total=0.

    The second evidence paper is in nobody's library, so membership already
    excludes the row: this pins the empty-response shape, not tenancy, and stays
    green if the ownership predicate is deleted. Ownership over papers both
    users hold is pinned by test_c5_01.

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
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/contradictions/scan returns 202 + job_id; task_registry carve-out.

    Seeds one summarized library paper so the caller passes the no-findings
    preflight and the scan is actually queued.

    # Verified: services/paper_ingestion/paper_ingestion/routers/contradictions.py:93
    # (scan_contradictions defers contradictions.scan via KIND_TO_TASK).
    """
    await _seed_summary_findings(
        contract_conn,
        contract_two_users.paper_id_a,
        user_id=None,
        finding="scan preflight finding",
        quote="scan preflight quote",
    )

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


async def test_c5_03b_scan_skips_without_enqueuing_when_no_findings(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """A caller with no summarized findings gets 202 skipped and no deferred job.

    contract_two_users seeds library papers but no paper_summaries rows, so the
    preflight COUNT (count_scannable_summaries) is exercised against the live
    schema and must return zero for user B.

    # Verified: services/paper_ingestion/paper_ingestion/routers/contradictions.py:90
    # (preflight returns status="skipped", job_id=None, reason="no_findings").
    """
    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"contradictions.scan": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
            resp = await c.post("/api/contradictions/scan", json={})

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("status") == "skipped"
    assert body.get("job_id") is None
    assert body.get("reason") == "no_findings"
    mock_task.defer_async.assert_not_awaited()


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

    # list_contradictions requires the full evidence pair in the caller's
    # library. Add all three papers so this test isolates the paper_id filter.
    await contract_conn.executemany(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        [
            (contract_two_users.user_a_id, paper_x_id),
            (contract_two_users.user_a_id, paper_y_id),
            (contract_two_users.user_a_id, paper_z_id),
        ],
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
    # normalized claim_topic (casefolded, punctuation collapsed, letters of
    # every script preserved).
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


async def test_c5_09_consensus_excludes_papers_outside_the_callers_library(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """User B cannot see a consensus cluster built only from user A's papers.

    Both evidence papers sit in user A's library alone, so the library-membership
    clauses exclude user B before ownership is ever evaluated. This test pins
    membership and nothing else -- deleting the ownership predicate leaves it
    green. Ownership of a cluster over papers BOTH users hold is pinned by
    test_c5_09c.

    # Verified: aggregate_consensus requires both evidence papers in user_library.
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


async def test_c5_09b_consensus_hides_mixed_library_pair(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """Consensus aggregation hides rows whose evidence pair is only partly in library."""
    user_a = contract_two_users.user_a_id
    owned = await _seed_library_paper(contract_conn, user_a, "mixed-consensus-owned")
    unowned = await _seed_library_paper(
        contract_conn, contract_two_users.user_b_id, "mixed-consensus-unowned"
    )
    await _seed_stance_row(
        contract_conn, owned, unowned, user_a, stance="supports", claim_topic="mixed claim"
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/consensus")

    assert resp.status_code == 200, resp.text[:300]
    topics = [claim["claim_topic"] for claim in resp.json()["claims"]]
    assert "mixed claim" not in topics


async def test_c5_09c_consensus_excludes_another_users_reading_of_shared_papers(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """Sharing both evidence papers must not share user A's assessment of them.

    Both papers sit in BOTH libraries, so the membership predicate admits user B
    for every row and only ownership can withhold the cluster.

    # Verified: services/paper_ingestion/paper_ingestion/services/
    # contradictions_persist.py:220 (aggregate_consensus constrains pc.user_id
    # alongside the two user_library membership checks).
    """
    user_a = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_a, "shared-consensus-p1")
    p2 = await _seed_library_paper(contract_conn, user_a, "shared-consensus-p2")
    for shared_paper_id in (p1, p2):
        await contract_conn.execute(
            """
            INSERT INTO user_library (user_id, paper_id, added_via)
            VALUES ($1, $2, 'manual_save')
            ON CONFLICT (user_id, paper_id) DO NOTHING
            """,
            contract_two_users.user_b_id,
            shared_paper_id,
        )
    await _seed_stance_row(
        contract_conn, p1, p2, user_a, stance="supports", claim_topic="shared claim"
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/consensus")
    assert resp_a.status_code == 200, resp_a.text[:300]
    topics_a = [claim["claim_topic"] for claim in resp_a.json()["claims"]]
    assert "shared claim" in topics_a, f"user A should see their own cluster: {topics_a}"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/consensus")
    assert resp_b.status_code == 200, resp_b.text[:300]
    topics_b = [claim["claim_topic"] for claim in resp_b.json()["claims"]]
    assert "shared claim" not in topics_b, (
        f"user B saw user A's assessment of shared papers: {topics_b}"
    )


async def test_c5_10b_unique_index_admits_one_row_per_owner(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """Two users scanning the same evidence each keep their own row.

    While the key spanned the deployment, the second user's insert collided with
    the first user's row and recorded nothing of their own, which is why a
    user-scoped read would have shown them an empty page.

    # Verified: db/migrations/0110_require_contradiction_owner.sql:56
    # keys idx_paper_contradictions_unique_quotes directly by user_id.
    """
    user_a = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_a, "per-owner-p1")
    p2 = await _seed_library_paper(contract_conn, user_a, "per-owner-p2")

    id_a = await _seed_contradiction(contract_conn, p1, p2, user_a)
    id_b = await _seed_contradiction(contract_conn, p1, p2, contract_two_users.user_b_id)

    assert id_a != id_b, "each owner must hold a distinct row for the same evidence"


async def test_c5_10c_normalized_write_reuses_legacy_whitespace_variant(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """The first normalized write after upgrade must not duplicate a raw legacy row."""
    user_id = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_id, "legacy-space-p1")
    p2 = await _seed_library_paper(contract_conn, user_id, "legacy-space-p2")
    legacy_id = await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_id,
        stance="supports",
        claim_topic="whether X holds",
        quote_a="Paper A  says\tX.",
        quote_b="Paper B says  not X.",
    )
    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=p1,
            title="Paper A",
            finding="Finding A",
            quote="Paper A says X.",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        b=VerifiedFinding(
            paper_id=p2,
            title="Paper B",
            finding="Finding B",
            quote="Paper B says not X.",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        score=0.8,
        reason="cross_reference",
    )
    parsed = ContradictionClassification(
        is_contradiction=False,
        stance="supports",
        claim_topic="whether X holds",
        explanation="Both findings affirm the claim.",
        quote_a="Paper A says X.",
        quote_b="Paper B says not X.",
        confidence=0.8,
    )

    persisted_id = await _persist_contradiction(
        contract_conn,
        candidate,
        parsed,
        page_a=1,
        page_b=2,
        model="contract-model",
        user_id=user_id,
    )
    row_count = await contract_conn.fetchval(
        """
        SELECT count(*) FROM paper_contradictions
        WHERE LEAST(paper_a_id, paper_b_id) = LEAST($1::integer, $2::integer)
          AND GREATEST(paper_a_id, paper_b_id) = GREATEST($1::integer, $2::integer)
          AND user_id = $3
        """,
        p1,
        p2,
        user_id,
    )

    assert persisted_id == legacy_id
    assert row_count == 1, "normalizing the new write must not create a second evidence row"


async def test_contradiction_generation_hides_old_evidence_and_admits_a_rescan(
    contract_two_users, contract_conn
):
    """The same evidence can be persisted again only for the new source generation."""
    from paper_ingestion.services.contradictions_persist import list_contradictions

    user_id = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_id, "contra-generation-p1")
    p2 = await _seed_library_paper(contract_conn, user_id, "contra-generation-p2")
    old_id = await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_id,
        stance="opposes",
        claim_topic="generation claim",
        quote_a="Paper A says X.",
        quote_b="Paper B says not X.",
    )
    before, _ = await list_contradictions(contract_conn, user_id=user_id, paper_id=p1)
    assert old_id in {row.id for row in before}

    await contract_conn.execute(
        "UPDATE papers SET content_generation = 1 WHERE id = $1",
        p1,
    )
    stale, _ = await list_contradictions(contract_conn, user_id=user_id, paper_id=p1)
    assert old_id not in {row.id for row in stale}

    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=p1,
            title="Paper A",
            finding="Finding A",
            quote="Paper A says X.",
            page_number=1,
            cross_reference_ids=frozenset(),
            content_generation=1,
        ),
        b=VerifiedFinding(
            paper_id=p2,
            title="Paper B",
            finding="Finding B",
            quote="Paper B says not X.",
            page_number=2,
            cross_reference_ids=frozenset(),
            content_generation=0,
        ),
        score=0.8,
        reason="lexical_overlap",
    )
    classification = ContradictionClassification(
        is_contradiction=True,
        stance="opposes",
        claim_topic="generation claim",
        explanation="The findings disagree.",
        quote_a="Paper A says X.",
        quote_b="Paper B says not X.",
        confidence=0.9,
    )
    current_id = await _persist_contradiction(
        contract_conn,
        candidate,
        classification,
        page_a=1,
        page_b=2,
        model="contract-model",
        user_id=user_id,
    )

    assert current_id is not None and current_id != old_id
    current, _ = await list_contradictions(contract_conn, user_id=user_id, paper_id=p1)
    assert {row.id for row in current} == {current_id}
    raw_count = await contract_conn.fetchval(
        """SELECT count(*) FROM paper_contradictions
           WHERE user_id = $1
             AND LEAST(paper_a_id, paper_b_id) = LEAST($2::integer, $3::integer)
             AND GREATEST(paper_a_id, paper_b_id) = GREATEST($2::integer, $3::integer)""",
        user_id,
        p1,
        p2,
    )
    assert raw_count == 2


async def test_operational_contradiction_views_rank_whitespace_variants_once(
    contract_two_users, contract_conn
):
    """Legacy whitespace variants remain stored but contribute one assessment."""
    from paper_ingestion.services.contradictions_persist import (
        aggregate_consensus,
        list_contradictions,
    )

    user_id = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_id, "contra-ranked-p1")
    p2 = await _seed_library_paper(contract_conn, user_id, "contra-ranked-p2")
    first_id = await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_id,
        stance="opposes",
        claim_topic="ranked claim",
        quote_a="Paper A  says\tX.",
        quote_b="Paper B says  not X.",
    )
    await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_id,
        stance="opposes",
        claim_topic="ranked claim",
        quote_a="Paper A says X.",
        quote_b="Paper B says not X.",
    )

    listed, total = await list_contradictions(contract_conn, user_id=user_id, paper_id=p1)
    claims, truncated = await aggregate_consensus(contract_conn, user_id=user_id)
    raw_count = await contract_conn.fetchval(
        """SELECT count(*) FROM paper_contradictions
           WHERE user_id = $1
             AND LEAST(paper_a_id, paper_b_id) = LEAST($2::integer, $3::integer)
             AND GREATEST(paper_a_id, paper_b_id) = GREATEST($2::integer, $3::integer)""",
        user_id,
        p1,
        p2,
    )

    assert raw_count == 2
    assert total == 1
    assert [row.id for row in listed] == [first_id]
    claim = next(item for item in claims if item.claim_topic == "ranked claim")
    assert claim.opposes == 1
    assert not truncated


async def test_0110_preserves_legacy_rows_and_requires_owner_for_new_evidence(
    contract_two_users, contract_conn
) -> None:
    """The ownership migration leaves legacy evidence byte-for-byte intact."""
    user_id = contract_two_users.user_a_id
    p1 = await _seed_library_paper(contract_conn, user_id, "migration-0110-p1")
    p2 = await _seed_library_paper(contract_conn, user_id, "migration-0110-p2")

    await contract_conn.execute(
        "ALTER TABLE paper_contradictions "
        "DROP CONSTRAINT IF EXISTS chk_paper_contradictions_user_id_present"
    )
    await contract_conn.execute(
        "ALTER TABLE paper_contradictions ALTER COLUMN user_id DROP NOT NULL"
    )
    await contract_conn.execute("DROP INDEX IF EXISTS idx_paper_contradictions_unique_quotes")
    await contract_conn.execute(
        """CREATE UNIQUE INDEX idx_paper_contradictions_unique_quotes
               ON paper_contradictions (
                   LEAST(paper_a_id, paper_b_id),
                   GREATEST(paper_a_id, paper_b_id),
                   md5(quote_a),
                   md5(quote_b),
                   COALESCE(user_id, 0)
               )"""
    )
    first_id = await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_id,
        stance="supports",
        claim_topic="whether X holds",
        quote_a="Paper A  says\tX.",
        quote_b="Paper B says  not X.",
    )
    second_id = await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_id,
        stance="opposes",
        claim_topic="whether X holds",
        quote_a="Paper A says X.",
        quote_b="Paper B says not X.",
    )
    ownerless_id = int(
        await contract_conn.fetchval(
            """INSERT INTO paper_contradictions (
                   paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
                   contradiction_type, explanation, confidence, user_id
               ) VALUES (
                   $1, $2, 'A', 'B', 'ownerless A', 'ownerless B',
                   'direct', 'unattributed', 0.5, NULL
               )
               RETURNING id""",
            p1,
            p2,
        )
    )
    legacy_ids = [first_id, second_id, ownerless_id]
    before = [
        dict(row)
        for row in await contract_conn.fetch(
            """SELECT id, user_id, quote_a, quote_b, finding_a, finding_b,
                      stance, claim_topic, paper_a_content_generation,
                      paper_b_content_generation
                 FROM paper_contradictions
                WHERE id = ANY($1::bigint[])
                ORDER BY id""",
            legacy_ids,
        )
    ]

    migration_sql = (
        Path(__file__).resolve().parents[4]
        / "db"
        / "migrations"
        / "0110_require_contradiction_owner.sql"
    ).read_text(encoding="utf-8")
    await contract_conn.execute(migration_sql)

    after = [
        dict(row)
        for row in await contract_conn.fetch(
            """SELECT id, user_id, quote_a, quote_b, finding_a, finding_b,
                      stance, claim_topic, paper_a_content_generation,
                      paper_b_content_generation
                 FROM paper_contradictions
                WHERE id = ANY($1::bigint[])
                ORDER BY id""",
            legacy_ids,
        )
    ]
    assert after == before
    assert len(after) == 3

    with pytest.raises(asyncpg.CheckViolationError):
        async with contract_conn.transaction():
            await contract_conn.execute(
                """INSERT INTO paper_contradictions (
                       paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
                       contradiction_type, explanation, confidence, user_id
                   ) VALUES (
                       $1, $2, 'A', 'B', 'new A', 'new B',
                       'direct', 'must be owned', 0.5, NULL
                   )""",
                p1,
                p2,
            )

    index_sql = await contract_conn.fetchval(
        """SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'idx_paper_contradictions_unique_quotes'"""
    )
    assert index_sql is not None
    assert "COALESCE" in index_sql
    assert "paper_a_content_generation" in index_sql
    assert "paper_b_content_generation" in index_sql

    await _seed_stance_row(
        contract_conn,
        p1,
        p2,
        user_id,
        stance="supports",
        claim_topic="new evidence",
        quote_a="normalized A",
        quote_b="normalized B",
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        async with contract_conn.transaction():
            await _seed_stance_row(
                contract_conn,
                p1,
                p2,
                user_id,
                stance="opposes",
                claim_topic="same evidence",
                quote_a="normalized A",
                quote_b="normalized B",
            )

    await contract_conn.execute(
        "UPDATE papers SET content_generation = 1 WHERE id = $1",
        p1,
    )
    await contract_conn.execute(
        """INSERT INTO paper_contradictions (
               paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
               contradiction_type, explanation, confidence, user_id,
               paper_a_content_generation, paper_b_content_generation
           ) VALUES (
               $1, $2, 'A3', 'B3', 'normalized A', 'normalized B',
               'direct', 'new generation', 0.7, $3, 1, 0
           )""",
        p1,
        p2,
        user_id,
    )


async def test_c5_10_unique_index_ignores_stance_and_topic_labels(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """For one owner the key is pair + quotes; a drifted label still collides.

    # Verified: db/migrations/0110_require_contradiction_owner.sql:56
    # keys the index on pair, quote hashes, and user_id, so a re-scan by the
    # SAME user of the same evidence with a drifted
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
