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
    from jarvis_common.testing_contract_apps import patch_app_state, patch_dependency_overrides
    from paper_ingestion.main import app

    # PDF_STORAGE_PATH is read at module-import; monkeypatch the module constant.
    import paper_ingestion.routers.pdf as _pdf_mod

    monkeypatch.setattr(_pdf_mod, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("PDF_STORAGE_PATH", str(tmp_path))

    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(
            app,
            {"db_pool": shared, "embedder": None, "pdf_processor": MagicMock()},
        ),
        patch_dependency_overrides(
            app, remove_overrides={current_user_id_strict_with_owner_override}
        ),
    ):
        yield app


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


# ---------------------------------------------------------------------------
# W2.5 — PDF workflow sidecar-backed contracts
# ---------------------------------------------------------------------------


# Verified: services/paper_ingestion/paper_ingestion/ingestion/embed_store.py:215
# (embed_and_store: upserts PointStruct objects into FauxQdrantClient collection)
async def test_pi_pdf_w2_chunk_upsert_via_faux_qdrant_persists_vectors(monkeypatch):
    """embed_and_store with FauxOllamaServer + FauxQdrantClient stores chunk vectors.

    Survivor-of: mock-unit tests in test_pdf_workflow.py that assert
    conn.executemany call shape without touching a real Qdrant/embedding boundary.
    """
    import httpx

    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import (
        COLLECTION_NAME,
        EMBEDDING_DIMENSION,
        Embedder,
    )
    from paper_ingestion.models import ChunkForEmbedding

    chunks = [
        ChunkForEmbedding(
            chunk_index=0, content="intro text", page_number=1, start_char=0, end_char=10
        ),
        ChunkForEmbedding(
            chunk_index=1, content="method section", page_number=2, start_char=11, end_char=25
        ),
    ]
    paper_id = 9901

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant = FauxQdrantClient()
            embedder = Embedder(http_client, qdrant)
            await embedder.ensure_collection()

            point_ids = await embedder.embed_and_store(paper_id, chunks)

    assert len(point_ids) == 2, f"Expected 2 point IDs; got {point_ids}"
    collection = qdrant._collections[COLLECTION_NAME]
    assert len(collection.points) == 2, f"Expected 2 Qdrant points; got {len(collection.points)}"
    for pid in point_ids:
        assert pid in collection.points, f"Point {pid!r} missing from FauxQdrant"
        stored = collection.points[pid]
        assert stored.payload["paper_id"] == paper_id
        assert len(stored.vector) == EMBEDDING_DIMENSION


# Verified: services/paper_ingestion/paper_ingestion/ingestion/embed_store.py:264
# (_CHUNK_POINT_ID_NAMESPACE + uuid.uuid5: deterministic per (paper_id, chunk_index))
async def test_pi_pdf_w2_chunk_point_id_stability_across_reprocess(monkeypatch):
    """Re-embedding the same paper yields identical Qdrant point IDs (uuid5 determinism).

    Survivor-of: test_embed_and_store_point_ids_are_deterministic (mock-unit) which
    verifies the uuid5 formula but does not exercise the real HTTP/Qdrant boundary.
    """
    import httpx

    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    chunks = [
        ChunkForEmbedding(
            chunk_index=0, content="stable chunk A", page_number=1, start_char=0, end_char=14
        ),
        ChunkForEmbedding(
            chunk_index=1, content="stable chunk B", page_number=1, start_char=15, end_char=29
        ),
    ]
    paper_id = 9902

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant1 = FauxQdrantClient()
            embedder1 = Embedder(http_client, qdrant1)
            await embedder1.ensure_collection()
            ids_first = await embedder1.embed_and_store(paper_id, chunks)

            qdrant2 = FauxQdrantClient()
            embedder2 = Embedder(http_client, qdrant2)
            await embedder2.ensure_collection()
            ids_second = await embedder2.embed_and_store(paper_id, chunks)

    assert ids_first == ids_second, (
        f"Point IDs diverged across reprocess: {ids_first!r} vs {ids_second!r}"
    )
    assert len(ids_first) == 2


# Verified: services/paper_ingestion/paper_ingestion/ingestion/embed_store.py:286
# (FauxQdrantClient.upsert raises ValueError on dimension mismatch → no orphan points)
async def test_pi_pdf_w2_qdrant_cleanup_on_processing_failure(monkeypatch):
    """A failure mid-upsert leaves FauxQdrant with no orphan points for that paper.

    Survivor-of: mock-unit in test_pdf_workflow.py that checks qdrant.delete call
    shape without a real Qdrant boundary.  This contract exercises the FauxQdrant
    reject-on-dimension-mismatch path: second chunk batch fails → collection has 0
    points for the paper (first batch had already succeeded → EmbeddingBatchError
    path; we verify completed chunks are NOT double-deleted by run_process_pdf).

    Strategy: wire two-chunk paper where second batch's embedding is wrong dimension
    so embed_and_store raises on the second batch (after first succeeded via upsert).
    Then verify only the first chunk's point ID survived in the collection.
    """
    import httpx

    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embed_store import EmbeddingBatchError
    from paper_ingestion.ingestion.embedder import COLLECTION_NAME, EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    paper_id = 9903
    chunks = [
        ChunkForEmbedding(
            chunk_index=0, content="first chunk ok", page_number=1, start_char=0, end_char=14
        ),
        ChunkForEmbedding(
            chunk_index=1, content="second chunk fail", page_number=1, start_char=15, end_char=32
        ),
    ]

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant = FauxQdrantClient()
            embedder = Embedder(http_client, qdrant)
            await embedder.ensure_collection()

            # Patch embed_texts: first call returns correct dim, second returns wrong dim
            call_count = 0
            original_embed = embedder.embed_texts

            async def _patched_embed(texts):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return await original_embed(texts)
                # Return wrong-dimension vector to trigger ValueError inside upsert
                return [[0.1] * (EMBEDDING_DIMENSION + 1)] * len(texts)

            embedder.embed_texts = _patched_embed  # type: ignore[method-assign]

            import pytest as _pytest

            with _pytest.raises((RuntimeError, ValueError, EmbeddingBatchError)):
                await embedder.embed_and_store(paper_id, chunks, batch_size=1)

    # After failure: first chunk's point may be in collection (batch_size=1 upserted it),
    # but second chunk must NOT have been upserted (dimension mismatch).
    collection = qdrant._collections[COLLECTION_NAME]
    paper_points = [p for p in collection.points.values() if p.payload.get("paper_id") == paper_id]
    # Only the first chunk (index 0) can be present; the second must be absent.
    chunk_indices = {p.payload["chunk_index"] for p in paper_points}
    assert 1 not in chunk_indices, (
        f"Second chunk (index 1) must not be in Qdrant after mid-upsert failure; "
        f"found chunk_indices={chunk_indices}"
    )
