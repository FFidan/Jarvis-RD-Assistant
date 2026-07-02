"""Tests for snapshot endpoint user-library scoping for local/uploaded PDFs.

Coverage:
  (a) Tenant B gets 404 for tenant A's uploaded/local paper snapshot.
  (b) A public-source paper snapshot is still served to a non-owner (D4).
  (c) Unknown paper_id returns opaque 404 (no existence oracle).
  (d) Path-traversal guard: secure_path blocks escaping paths (400).
"""

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn


def _paper_row(source_type: str, in_library: bool, discovered_by: int | None = None) -> dict:
    return {
        "source_type": source_type,
        "in_library": in_library,
        "discovered_by": discovered_by,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _snap_app(tmp_path):
    """App with mocked DB/auth and a real PNG on disk for paper_id=1, page=1."""
    import paper_ingestion.routers.snapshots as snap_mod
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    snap_dir = tmp_path / "1"
    snap_dir.mkdir()
    (snap_dir / "page_1.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    pool, conn = make_pool_and_conn()
    app.state.db_pool = pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None

    original_path = snap_mod.SNAPSHOT_STORAGE_PATH
    snap_mod.SNAPSHOT_STORAGE_PATH = str(tmp_path)

    yield app, conn

    snap_mod.SNAPSHOT_STORAGE_PATH = original_path
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# (a) Local paper blocked for non-owner
# ---------------------------------------------------------------------------


async def test_local_paper_snapshot_denied_to_non_owner(_snap_app):
    """Tenant B receives an opaque 404 for tenant A's local/uploaded paper."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2  # Tenant B
    conn.fetchrow.return_value = _paper_row("local", in_library=False, discovered_by=1)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 404
    # Opaque — must not leak existence via different wording
    assert "library" not in resp.text.lower()


async def test_local_paper_snapshot_allowed_for_owner(_snap_app):
    """Owner of a local paper can retrieve its snapshot."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1  # Owner
    conn.fetchrow.return_value = _paper_row("local", in_library=True)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


# ---------------------------------------------------------------------------
# (a') ZOTERO (private-origin) papers are ownership-scoped, not public
# ---------------------------------------------------------------------------


async def test_zotero_paper_snapshot_denied_to_non_discoverer(_snap_app):
    """A ZOTERO paper discovered by user 1 is an opaque 404 for user 2.

    ZOTERO is private-origin (not public corpus); without a library row a
    non-discoverer must not be served the snapshot via its enumerable id.
    """
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2  # Non-discoverer
    conn.fetchrow.return_value = _paper_row("zotero", in_library=False, discovered_by=1)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 404
    # Opaque — must not leak existence via different wording
    assert "library" not in resp.text.lower()


async def test_zotero_paper_snapshot_allowed_for_discoverer(_snap_app):
    """The caller who discovered a ZOTERO paper can retrieve its snapshot."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1  # Discoverer
    conn.fetchrow.return_value = _paper_row("zotero", in_library=False, discovered_by=1)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


async def test_private_paper_snapshot_allowed_via_library(_snap_app):
    """A private paper discovered by another user but in the caller's library is served."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2  # Not the discoverer
    conn.fetchrow.return_value = _paper_row("zotero", in_library=True, discovered_by=1)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


# ---------------------------------------------------------------------------
# (b) Public-source papers remain shared per D4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_type", ["arxiv", "semantic_scholar", "openalex", "pubmed"])
async def test_public_source_snapshot_accessible_to_non_owner(_snap_app, source_type):
    """Public-corpus papers are served to any authenticated user (D4 preserved)."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 99  # Not in library
    conn.fetchrow.return_value = _paper_row(source_type, in_library=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


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
    # Use a public-source paper so the DB scoping gate does not fire first.
    conn.fetchrow.return_value = _paper_row("arxiv", in_library=False)

    def _escape(*_args, **_kwargs):
        raise ValueError("path escapes base directory")

    monkeypatch.setattr(snap_mod, "secure_path", _escape)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/snapshots/1/1")

    # Guard must fire and return 400 — NOT 404 or 200.
    assert resp.status_code == 400
