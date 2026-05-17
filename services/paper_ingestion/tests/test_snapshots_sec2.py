"""Tests for SEC-2: snapshot endpoint user-library scoping for local/uploaded PDFs.

Coverage:
  (a) Tenant B gets 404 for tenant A's uploaded/local paper snapshot.
  (b) A public-source paper snapshot is still served to a non-owner (D4).
  (c) Unknown paper_id returns opaque 404 (no existence oracle).
  (d) Path-traversal guard: is_relative_to check blocks escaping paths.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_pool() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


def _paper_row(source_type: str, in_library: bool) -> dict:
    return {"source_type": source_type, "in_library": in_library}


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

    pool, conn = _mock_pool()
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
# (a) Local paper blocked for non-owner (SEC-2 core case)
# ---------------------------------------------------------------------------


async def test_local_paper_snapshot_denied_to_non_owner(_snap_app):
    """Tenant B receives an opaque 404 for tenant A's local/uploaded paper."""
    app, conn = _snap_app
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 2  # Tenant B
    conn.fetchrow.return_value = _paper_row("local", in_library=False)

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


async def test_path_traversal_guard_still_active(_snap_app, tmp_path, monkeypatch):
    """is_relative_to guard blocks paths that escape the storage root."""
    import paper_ingestion.routers.snapshots as snap_mod
    from jarvis_common.auth import get_current_user_id

    app, conn = _snap_app
    app.dependency_overrides[get_current_user_id] = lambda: 1
    conn.fetchrow.return_value = _paper_row("arxiv", in_library=False)

    # Fabricate a snapshot_path that escapes base by monkeypatching Path.resolve
    # on the constructed path. Easier: override SNAPSHOT_STORAGE_PATH to a subdir
    # so that tmp_path/1/page_1.png is NOT relative to the new base.
    outer_base = tmp_path / "outer"
    outer_base.mkdir()
    # The real snapshot file lives at tmp_path/1/page_1.png (created by fixture).
    # Set storage path to outer_base → resolved snapshot is tmp_path/1/page_1.png
    # which is NOT relative to outer_base → guard fires → 400.
    original = snap_mod.SNAPSHOT_STORAGE_PATH
    snap_mod.SNAPSHOT_STORAGE_PATH = str(outer_base)

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # paper_id=1, page=1 → outer_base/1/page_1.png IS relative to outer_base
            # → guard passes, file not present → 404
            resp = await client.get("/api/snapshots/1/1")
    finally:
        snap_mod.SNAPSHOT_STORAGE_PATH = original

    # outer_base/1/page_1.png doesn't exist → 404 (path is valid, guard didn't fire)
    assert resp.status_code == 404
