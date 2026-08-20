"""Tests for snapshot authorization through the central paper policy.

Coverage:
  (a) Private papers require explicit caller-library membership.
  (b) Persisted-public paper snapshots are served to authenticated users.
  (c) Unknown paper_id returns opaque 404 (no existence oracle).
  (d) Path-traversal guard: secure_path blocks escaping paths (400).
  (e) The routes sharing the guard consult it before touching storage, and
      still serve a paper the guard admits.

The guard's own predicate is exercised against a real database in
``tests/contract/test_pi_pdf_contract.py``; here the lookup is mocked, so these
tests pin how each route reacts to the guard's verdict.
"""

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn


def _visible_paper_row(source_type: str = "local") -> dict[str, str]:
    """Represent a row returned after the database visibility predicate passes."""
    return {"source_type": source_type}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _snap_app(tmp_path):
    """App with mocked DB/auth and real files on disk for paper_id=1, page=1.

    Both the page PNG and the raw PDF are present, so a route that returns 404
    can only have been stopped by the shared guard.
    """
    import paper_ingestion.routers.pdf_files as pdfs_mod
    import paper_ingestion.routers.snapshots as snap_mod
    from jarvis_common.auth import verify_api_key
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    snap_dir = tmp_path / "1"
    snap_dir.mkdir()
    (snap_dir / "page_1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "1.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    pool, conn = make_pool_and_conn()

    # The storage-path module swaps are a seam the shared helper deliberately
    # does not cover; restore them manually alongside the helper's restore.
    original_path = snap_mod.SNAPSHOT_STORAGE_PATH
    original_pdf_path = pdfs_mod.PDF_STORAGE_PATH
    snap_mod.SNAPSHOT_STORAGE_PATH = str(tmp_path)
    pdfs_mod.PDF_STORAGE_PATH = str(tmp_path)
    try:
        with patch_pi_test_app(
            pool,
            app=app,
            get_db_pool=get_db_pool,
            limiter=limiter,
            options=PITestAppOptions(
                remove_identity_overrides=False,
                override_db_dependency=True,
                disable_limiter=True,
                dependency_overrides={verify_api_key: lambda: None},
            ),
        ):
            yield app, conn
    finally:
        snap_mod.SNAPSHOT_STORAGE_PATH = original_path
        pdfs_mod.PDF_STORAGE_PATH = original_pdf_path


# ---------------------------------------------------------------------------
# (a) Local paper blocked for non-owner
# ---------------------------------------------------------------------------


async def test_local_paper_snapshot_denied_to_non_owner(_snap_app):
    """Tenant B receives an opaque 404 for tenant A's local/uploaded paper."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2  # Tenant B
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 404
    # Opaque — must not leak existence via different wording
    assert "library" not in resp.text.lower()


async def test_unattributed_private_snapshot_denied_without_library(_snap_app):
    """Erasing the discoverer cannot turn a private-origin snapshot public."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 404


async def test_unattributed_zotero_snapshot_allowed_via_library(_snap_app):
    """A surviving library membership still authorizes an erased discoverer's item."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2
    conn.fetchrow.return_value = _visible_paper_row("zotero")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200


async def test_local_paper_snapshot_allowed_for_owner(_snap_app):
    """A caller with a local paper in their library can retrieve its snapshot."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1  # Owner
    conn.fetchrow.return_value = _visible_paper_row("local")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


# ---------------------------------------------------------------------------
# (a') Provenance and discoverer metadata do not grant access
# ---------------------------------------------------------------------------


async def test_zotero_paper_snapshot_denied_to_non_discoverer(_snap_app):
    """A ZOTERO paper discovered by user 1 is an opaque 404 for user 2.

    ZOTERO is private-origin (not public corpus); without a library row a
    non-discoverer must not be served the snapshot via its enumerable id.
    """
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2  # Non-discoverer
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 404
    # Opaque — must not leak existence via different wording
    assert "library" not in resp.text.lower()


async def test_zotero_paper_snapshot_denied_to_discoverer_without_library(_snap_app):
    """Discoverer attribution alone does not authorize a private snapshot."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1  # Discoverer
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 404


async def test_private_paper_snapshot_allowed_via_library(_snap_app):
    """A private paper in the caller's library is served regardless of discoverer."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2  # Not the discoverer
    conn.fetchrow.return_value = _visible_paper_row("zotero")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


# ---------------------------------------------------------------------------
# (b) Persisted-public papers remain shared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_type", ["arxiv", "semantic_scholar", "openalex", "pubmed"])
async def test_persisted_public_snapshot_accessible_to_authenticated_user(_snap_app, source_type):
    """A row accepted by the persisted-public predicate is served regardless of source."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 99  # Not in library
    conn.fetchrow.return_value = _visible_paper_row(source_type)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"

    visibility_query = str(conn.fetchrow.await_args.args[0])
    assert "p.visibility_scope = 'public'" in visibility_query
    assert "user_library" in visibility_query
    assert "discovered_by" not in visibility_query
    assert "source_type IN" not in visibility_query


# ---------------------------------------------------------------------------
# (c) Unknown paper_id → opaque 404
# ---------------------------------------------------------------------------


async def test_unknown_paper_id_returns_404(_snap_app):
    """A paper_id not in the DB returns 404 — no existence oracle."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1
    conn.fetchrow.return_value = None  # Paper not in DB

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/999/1")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# (d) Path-traversal guard
# ---------------------------------------------------------------------------


async def test_path_traversal_guard_still_active(_snap_app, monkeypatch):
    """secure_path guard returns HTTP 400 when the resolved path escapes storage root.

    The production guard is:
        try:
            snapshot_path = secure_path(SNAPSHOT_STORAGE_PATH, str(paper_id), ...)
        except ValueError:
            raise HTTPException(400, "Invalid path")

    Since paper_id and page are typed ``int``, FastAPI rejects non-numeric path
    segments before the handler runs, so traversal can only originate from a
    compromised storage path or future refactor.  We monkeypatch the router's
    ``secure_path`` reference to raise ``ValueError`` to directly exercise the
    guard's 400 branch without relying on filesystem layout.

    Non-tautology guarantee: removing the ``secure_path`` guard from the
    production handler would cause this test to receive 200 (file found) or 404
    (file missing) instead of 400, so the assertion fails.
    """
    import paper_ingestion.routers.snapshots as snap_mod
    from jarvis_common.auth import get_current_user_id

    app, conn = _snap_app
    app.dependency_overrides[get_current_user_id] = lambda: 1
    # Represent a paper accepted by the central visibility predicate so the
    # filesystem guard is the first failing boundary.
    conn.fetchrow.return_value = _visible_paper_row("arxiv")

    def _escape(*_args, **_kwargs):
        raise ValueError("path escapes base directory")

    monkeypatch.setattr(snap_mod, "secure_path", _escape)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    # Guard must fire and return 400 — NOT 404 or 200.
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# (e) The routes sharing the guard follow its verdict
# ---------------------------------------------------------------------------


async def test_shared_routes_serve_a_paper_the_guard_admits(_snap_app):
    """A paper the guard admits still serves its PDF, snapshot, and highlights.

    Tightening the guard's predicate must not narrow what an ordinary local
    upload can reach, so all three shared routes are exercised together.
    """
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1
    conn.fetchrow.return_value = _visible_paper_row("local")
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        pdf = await client.get("/api/pdfs/1")
        snapshot = await client.get("/api/snapshots/1/1")
        highlights = await client.get("/api/papers/1/highlights")

    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert snapshot.status_code == 200
    assert highlights.status_code == 200
    assert highlights.json() == []


async def test_shared_routes_stop_at_the_guard_before_reading_storage(_snap_app):
    """Every shared route returns an opaque 404 when the guard admits no row.

    Both files exist on disk for paper 1, so a 404 here can only come from the
    guard. This is the boundary a paper with no stored PDF record reaches: the
    lookup matches nothing, and no route falls back to the storage directory.
    """
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        pdf = await client.get("/api/pdfs/1")
        snapshot = await client.get("/api/snapshots/1/1")
        highlights = await client.get("/api/papers/1/highlights")

    assert pdf.status_code == 404
    assert snapshot.status_code == 404
    assert highlights.status_code == 404
    assert "library" not in pdf.text.lower()
