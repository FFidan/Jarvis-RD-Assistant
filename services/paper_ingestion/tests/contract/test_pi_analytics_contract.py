"""Analytics router contract tests — Cluster 7.

Covers GET /api/analytics/missing-foundational, POST /api/analytics/fetch-and-process,
and GET /api/analytics/feedback-summary. Replaces the 9 mock-unit tests in
services/paper_ingestion/tests/test_analytics_router.py with survivor citations:

  test_missing_foundational_returns_ranked_stub_rows
  test_missing_foundational_filters_by_user_library
  test_fetch_and_process_local_pdf_promotes_stub_and_enqueues_process
  test_fetch_and_process_pdf_url_promotes_stub_and_enqueues_analyze
  test_fetch_and_process_without_pdf_promotes_stub_but_does_not_enqueue
  test_fetch_and_process_missing_or_non_stub_row_returns_404
  test_feedback_summary_returns_correct_shape
  test_feedback_summary_empty_table
  test_feedback_summary_filters_by_user_id
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool
from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    make_contract_client as _make_client,
    patch_pi_test_app,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app as pi_app

    shared = SharedConnPool(contract_conn)
    with patch_pi_test_app(
        shared,
        app=pi_app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=True,
            state_overrides={"embedder": None},
        ),
    ) as app:
        yield app


async def _seed_stub_cited_by(conn, user_paper_id: int, *, cited_by_count: int = 5) -> int:
    """Seed a stub paper cited by ``user_paper_id`` via paper_citations."""
    stub_id = await conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, metadata,
                            citation_count, pdf_downloaded, discovered_by)
        VALUES ('stub-foundational-1', 'arxiv', 'Foundational paper', ARRAY['A. Cite'],
                'https://example.test/foundational',
                '{"stub": "true"}'::jsonb, $1, FALSE, NULL)
        RETURNING id
        """,
        cited_by_count,
    )
    await conn.execute(
        "INSERT INTO paper_citations (source_paper_id, cited_paper_id) VALUES ($1, $2)",
        user_paper_id,
        stub_id,
    )
    return int(stub_id)


# ---------------------------------------------------------------------------
# GET /api/analytics/missing-foundational — ranked + scoped to user_library
# ---------------------------------------------------------------------------


async def test_c7_01_missing_foundational_ranked_scoped_to_user_library(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/analytics/missing-foundational ranks stub papers cited by the caller's library; IDOR-scoped.

    User A's seeded paper cites a stub paper; A sees it in their list; B sees empty list.

    # Verified: services/paper_ingestion/paper_ingestion/routers/analytics.py:40
    # (get_missing_foundational JOINs paper_citations + user_library scoped to caller).
    """
    stub_id = await _seed_stub_cited_by(
        contract_conn, contract_two_users.paper_id_a, cited_by_count=12
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/analytics/missing-foundational")
    assert resp_a.status_code == 200, resp_a.text[:300]
    rows_a = resp_a.json()
    paper_ids_a = [r["paper_id"] for r in rows_a]
    assert stub_id in paper_ids_a, f"Stub {stub_id} should appear for user A; got {paper_ids_a}"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/analytics/missing-foundational")
    assert resp_b.status_code == 200, resp_b.text[:300]
    paper_ids_b = [r["paper_id"] for r in resp_b.json()]
    assert stub_id not in paper_ids_b, (
        f"IDOR leak: user B saw stub {stub_id} cited only by user A's library: {paper_ids_b}"
    )


# ---------------------------------------------------------------------------
# POST /api/analytics/fetch-and-process — local PDF → queued
# ---------------------------------------------------------------------------


async def test_c7_02_fetch_and_process_local_pdf_returns_queued(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/analytics/fetch-and-process with a local-PDF stub enqueues paper.process.

    Seeds a stub paper with pdf_downloaded=True + pdf_local_path; verifies the
    handler returns 202 + status="queued" + job_id; asserts the stub flag cleared.

    # Verified: services/paper_ingestion/paper_ingestion/routers/analytics.py:88
    # (fetch_and_process_foundational: local-PDF branch defers paper.process).
    """
    stub_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, metadata,
                            pdf_downloaded, pdf_local_path, discovered_by)
        VALUES ('stub-local-pdf', 'arxiv', 'Local PDF stub', ARRAY['A'],
                'https://example.test/p', '{"stub": "true"}'::jsonb,
                TRUE, '/tmp/dummy.pdf', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        stub_id,
    )

    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"paper.process": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post(
                "/api/analytics/fetch-and-process",
                json={"paper_id": stub_id},
            )

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("status") == "queued"
    assert body.get("job_id"), f"Missing job_id: {body}"

    # Stub flag cleared
    metadata = await contract_conn.fetchval(
        "SELECT metadata FROM papers WHERE id = $1",
        stub_id,
    )
    assert metadata.get("stub") == "false", (
        f"Stub flag should be cleared after fetch-and-process; got metadata={metadata!r}"
    )

    mock_task.defer_async.assert_awaited_once()
    call_kwargs = mock_task.defer_async.call_args.kwargs
    assert call_kwargs["paper_id"] == stub_id
    assert str(call_kwargs["user_id"]) == str(contract_two_users.user_a_id)


# ---------------------------------------------------------------------------
# POST /api/analytics/fetch-and-process — missing or non-stub → 404
# ---------------------------------------------------------------------------


async def test_c7_03_fetch_and_process_missing_stub_returns_404(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/analytics/fetch-and-process for nonexistent paper_id returns 404.

    # Verified: services/paper_ingestion/paper_ingestion/routers/analytics.py:88
    # (initial SELECT WHERE id AND metadata->>'stub'='true' returns no row → 404).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/analytics/fetch-and-process",
            json={"paper_id": 9_999_999},
        )
    assert resp.status_code == 404, resp.text[:300]
    assert "stub" in resp.text.lower() or "not found" in resp.text.lower(), (
        f"404 detail should reference stub/not-found; got: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# GET /api/analytics/feedback-summary — shape + user isolation
# ---------------------------------------------------------------------------


async def test_c7_04_feedback_summary_shape_and_user_isolation(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/analytics/feedback-summary returns {top_positive, top_negative} scoped to caller.

    Seeds recommendation_feedback rows for user A only; A sees their rows, B sees empty.

    # Verified: services/paper_ingestion/paper_ingestion/routers/analytics.py:141
    # (feedback_summary scoped via WHERE rf.user_id = $1).
    """
    await contract_conn.execute(
        """
        INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
        VALUES ($1, $2, 'positive', 'feed_thumbs')
        """,
        contract_two_users.paper_id_a,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/analytics/feedback-summary")
    assert resp_a.status_code == 200, resp_a.text[:300]
    body_a = resp_a.json()
    for key in ("top_positive", "top_negative"):
        assert key in body_a, f"Missing key {key!r}: {body_a}"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/analytics/feedback-summary")
    assert resp_b.status_code == 200, resp_b.text[:300]
    body_b = resp_b.json()
    # User B has no recommendation_feedback rows seeded → both lists are empty
    assert body_b.get("top_positive") == [], (
        f"IDOR leak: user B saw top_positive={body_b.get('top_positive')!r}"
    )


# ---------------------------------------------------------------------------
# POST /api/analytics/fetch-and-process — no PDF → status="no_pdf"
# ---------------------------------------------------------------------------


async def test_c7_05_fetch_and_process_no_pdf_returns_no_pdf_status(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/analytics/fetch-and-process for a stub with no PDF returns status="no_pdf".

    Seeds a stub with pdf_url=NULL and pdf_downloaded=FALSE; verifies the handler
    promotes the stub but doesn't enqueue + returns "no_pdf".

    # Verified: services/paper_ingestion/paper_ingestion/routers/analytics.py:88
    # (fetch_and_process_foundational: no-PDF branch returns no_pdf with message).
    """
    stub_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, metadata,
                            pdf_url, pdf_downloaded, discovered_by)
        VALUES ('stub-no-pdf', 'arxiv', 'No PDF stub', ARRAY['A'],
                'https://example.test/p', '{"stub": "true"}'::jsonb,
                NULL, FALSE, $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        stub_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/analytics/fetch-and-process",
            json={"paper_id": stub_id},
        )

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("status") == "no_pdf", (
        f"Expected status=no_pdf when no PDF available; got {body}"
    )
    assert body.get("job_id") is None, f"job_id should be None for no_pdf branch; got {body}"


# ---------------------------------------------------------------------------
# POST /api/analytics/fetch-and-process — enqueue failure reverts stub flag
# ---------------------------------------------------------------------------


async def test_c7_06_fetch_and_process_enqueue_failure_reverts_stub_flag(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """A defer_async failure must revert metadata.stub so the row stays retryable.

    Promotes a local-PDF stub, but the enqueue raises. The handler must return
    503 AND leave metadata.stub='true' (re-flipped) so the opening
    SELECT … metadata->>'stub'='true' still matches on the next attempt.
    """
    stub_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, metadata,
                            pdf_downloaded, pdf_local_path, discovered_by)
        VALUES ('stub-enqueue-fail', 'arxiv', 'Enqueue-fail stub', ARRAY['A'],
                'https://example.test/p', '{"stub": "true"}'::jsonb,
                TRUE, '/tmp/dummy.pdf', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        stub_id,
    )

    failing_task = AsyncMock()
    failing_task.defer_async = AsyncMock(side_effect=RuntimeError("broker unreachable"))
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"paper.process": failing_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/analytics/fetch-and-process", json={"paper_id": stub_id})

    assert resp.status_code == 503, resp.text[:300]
    metadata = await contract_conn.fetchval("SELECT metadata FROM papers WHERE id = $1", stub_id)
    assert metadata.get("stub") == "true", (
        f"Stub flag must be reverted on enqueue failure so retry still matches; got {metadata!r}"
    )
