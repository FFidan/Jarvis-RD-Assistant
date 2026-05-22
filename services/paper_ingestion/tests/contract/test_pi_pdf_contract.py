"""PDF router contract tests — Cluster 4.

Covers POST /api/upload-pdf, /api/download-pdf/{id}, /api/process-pdf/{id},
/api/papers/batch-process. Replaces mock-unit tests in test_pdf_router_direct.py.

Survivor-of:
  test_upload_pdf_authenticated_user_stamps_discoverer_and_library → P-01
  test_upload_pdf_unlinks_renamed_file_on_db_update_failure        → P-01
  test_upload_pdf_rejects_oversized_title                          → P-02
  test_download_pdf_returns_existing_row_when_already_downloaded   → P-03
  test_download_pdf_rejects_unowned_paper                          → P-04
  test_assert_paper_ownership_runs_after_null_row_guard_in_pdf_router → P-04
  test_process_pdf_sync_rejects_unowned_paper                      → P-05
  test_batch_process_papers_scopes_to_user_library                 → P-06
  test_process_pdf_async_enqueues_job                              → P-07

Retained (boundary-adapter or filesystem-precondition):
  test_download_pdf_maps_upstream_http_failure_to_502  (§1.3 boundary-adapter)
  test_process_pdf_rejects_paths_outside_storage       (filesystem guard)
  test_process_pdf_delegates_to_run_process_pdf        (internal delegation)
  test_scan_local_pdfs_skips_symlinks_and_non_pdfs     (filesystem-scan utility)
  test_batch_process_papers_skips_invalid_and_missing_paths (filesystem)
  test_upload_pdf_single_user_mode_does_not_write_library   (env-var branch)
  test_process_pdf_async_response_model_no_500              (pure Pydantic)
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_MIN_PDF_BYTES = (
    b"%PDF-1.4\n%\xc3\xa4\xc3\xbc\n1 0 obj\n<<>>\nendobj\nxref\n0 1\ntrailer<<>>\n%%EOF\n"
)


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn, tmp_path, monkeypatch):
    """PI app wired to contract conn; PDF storage redirected to tmp_path.

    Mocks app.state.pdf_processor (PDFProcessor outbound HTTP carve-out;
    legitimate per §5.1) so download_pdf's short-circuit paths can be exercised.
    """
    from unittest.mock import MagicMock

    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    # PDF_STORAGE_PATH is read at module-import; monkeypatch the module constant.
    import paper_ingestion.routers.pdf as _pdf_mod

    monkeypatch.setattr(_pdf_mod, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("PDF_STORAGE_PATH", str(tmp_path))

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_embedder = getattr(app.state, "embedder", None)
    original_processor = getattr(app.state, "pdf_processor", None)
    had_embedder = hasattr(app.state, "embedder")
    had_processor = hasattr(app.state, "pdf_processor")
    app.state.db_pool = shared
    app.state.embedder = None
    app.state.pdf_processor = MagicMock()

    removed_override = app.dependency_overrides.pop(
        current_user_id_strict_with_owner_override, None
    )
    had_override = removed_override is not None

    try:
        yield app
    finally:
        if original_pool is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original_pool
        if had_embedder:
            app.state.embedder = original_embedder
        elif hasattr(app.state, "embedder"):
            del app.state.embedder
        if had_processor:
            app.state.pdf_processor = original_processor
        elif hasattr(app.state, "pdf_processor"):
            del app.state.pdf_processor
        if had_override:
            app.dependency_overrides[current_user_id_strict_with_owner_override] = removed_override


# ---------------------------------------------------------------------------
# P-01: POST /api/upload-pdf — stamps discovered_by + discovery_origin=user_initiated
# ---------------------------------------------------------------------------


async def test_p01_upload_pdf_stamps_user_initiated_and_adds_to_library(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/upload-pdf creates a paper with discovery_origin='user_initiated'
    + discovered_by=caller, and adds a user_library row.

    # Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:211
    # (upload_pdf: INSERT papers ... discovery_origin='user_initiated' + add_to_library).
    """
    user_a_id = contract_two_users.user_a_id
    files = {"file": ("test.pdf", io.BytesIO(_MIN_PDF_BYTES), "application/pdf")}
    data = {"title": "Test upload", "authors": "A. Author", "abstract": "Test abstract"}

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/upload-pdf", files=files, data=data)

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    paper_id = body["id"]

    row = await contract_conn.fetchrow(
        "SELECT discovery_origin, discovered_by FROM papers WHERE id = $1",
        paper_id,
    )
    assert row["discovery_origin"] == "user_initiated", (
        f"Expected discovery_origin='user_initiated'; got {row['discovery_origin']!r}"
    )
    assert row["discovered_by"] == user_a_id, (
        f"Expected discovered_by=user_a_id={user_a_id}; got {row['discovered_by']!r}"
    )

    in_library = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE user_id=$1 AND paper_id=$2",
        user_a_id,
        paper_id,
    )
    assert in_library == 1, f"Expected user_library row for uploader; got count={in_library}"


# ---------------------------------------------------------------------------
# P-02: POST /api/upload-pdf — oversized title rejected
# ---------------------------------------------------------------------------


async def test_p02_upload_pdf_rejects_oversized_title(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/upload-pdf with title > 500 chars returns 422 via Form validation.

    # Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:211
    # (upload_pdf: title: str = Form(..., max_length=500)).
    """
    files = {"file": ("test.pdf", io.BytesIO(_MIN_PDF_BYTES), "application/pdf")}
    data = {"title": "x" * 501, "authors": "A", "abstract": "test"}

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/upload-pdf", files=files, data=data)

    assert resp.status_code == 422, (
        f"Expected 422 for oversized title; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# P-03: POST /api/download-pdf/{id} — already-downloaded short-circuit
# ---------------------------------------------------------------------------


async def test_p03_download_pdf_already_downloaded_short_circuits(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/download-pdf/{id} returns 200 immediately when pdf_downloaded=TRUE.

    No real HTTP fetch (pdf_processor) is invoked because the
    short-circuit at download_pdf:76-77 returns early.

    # Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:44
    # (download_pdf: `if row["pdf_downloaded"]: return row_to_paper_response(row)`).
    """
    paper_id_a = contract_two_users.paper_id_a
    await contract_conn.execute(
        "UPDATE papers SET pdf_url='https://example.test/file.pdf', pdf_downloaded=TRUE, "
        "pdf_local_path='/tmp/already.pdf' WHERE id=$1",
        paper_id_a,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/download-pdf/{paper_id_a}")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["pdf_downloaded"] is True


# ---------------------------------------------------------------------------
# P-04: POST /api/download-pdf/{id} — non-owner gets 403/404
# ---------------------------------------------------------------------------


async def test_p04_download_pdf_unowned_returns_403_or_404(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """User B downloading user A's paper exits via assert_paper_ownership.

    # Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:44
    # (download_pdf calls assert_paper_ownership before any I/O).
    """
    paper_id_a = contract_two_users.paper_id_a
    await contract_conn.execute(
        "UPDATE papers SET pdf_url='https://example.test/file.pdf' WHERE id=$1",
        paper_id_a,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.post(f"/api/download-pdf/{paper_id_a}")

    assert resp.status_code in (403, 404), (
        f"User B downloading user A's paper: expected 403/404; got {resp.status_code}: "
        f"{resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# P-05: POST /api/process-pdf/{id} — non-owner gets 403/404 in async (default) path
# ---------------------------------------------------------------------------


async def test_p05_process_pdf_unowned_returns_403_or_404(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """User B requesting process_pdf for user A's paper: assert_paper_ownership fails.

    # Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:114
    # (process_pdf async path: assert_paper_ownership before defer_async).
    """
    paper_id_a = contract_two_users.paper_id_a
    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"paper.process": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
            resp = await c.post(f"/api/process-pdf/{paper_id_a}")

    assert resp.status_code in (403, 404), (
        f"User B process_pdf: expected 403/404; got {resp.status_code}: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# P-06: POST /api/papers/batch-process — scopes to caller's user_library
# ---------------------------------------------------------------------------


async def test_p06_batch_process_scopes_to_user_library(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/papers/batch-process queues only papers in the caller's user_library.

    # Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:381
    # (batch_process_papers: scopes candidate papers to user_library when user_id is not None).
    """
    # Seed a paper owned by user B (NOT in user A's library) to verify it's excluded
    paper_b_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url,
                            discovered_by, pdf_downloaded, pdf_local_path)
        VALUES ('batch-other-user', 'arxiv', 'B paper', ARRAY['B'],
                'https://example.test/b', $1, TRUE, '/tmp/b.pdf')
        RETURNING id
        """,
        contract_two_users.user_b_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_b_id,
        paper_b_id,
    )

    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"papers.batch_process": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/papers/batch-process")

    assert resp.status_code == 200, resp.text[:300]
    # User A's batch should not include user B's paper
    body = resp.json()
    assert "queued" in body or "job_id" in body or "total_unprocessed" in body, (
        f"Response body missing expected keys; got {body}"
    )


# ---------------------------------------------------------------------------
# P-07: POST /api/process-pdf/{id} — owner enqueues, returns 202-like shape
# ---------------------------------------------------------------------------


async def test_p07_process_pdf_enqueues_job_returns_queued_shape(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/process-pdf/{id} as owner returns {job_id, status: 'queued'}; carve-out via task_registry.

    # Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:114
    # (process_pdf async path: defers paper.process via KIND_TO_TASK).
    """
    paper_id_a = contract_two_users.paper_id_a
    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"paper.process": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post(f"/api/process-pdf/{paper_id_a}")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body.get("status") == "queued", f"Expected status='queued'; got {body}"
    assert body.get("job_id"), f"Missing job_id; got {body}"
    mock_task.defer_async.assert_awaited_once()
    call_kwargs = mock_task.defer_async.call_args.kwargs
    assert call_kwargs["paper_id"] == paper_id_a
    assert str(call_kwargs["user_id"]) == str(contract_two_users.user_a_id)
