"""Contract tests for the admin Backup router (GET/POST /api/admin/backups).

Auth: routers/backups.py is included with ``dependencies=[]`` (no global
verify_api_key) and ``router.auth_exempt=True``; every route is gated by
``Depends(require_admin)`` (session role=='admin' only). We seed a real
users+sessions row so SessionMiddleware sets request.state.user_role='admin'.

The backup directory + trigger sentinel are pointed at a tmp_path via the
module-level constants so no real /backups mount is required.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_admin_user(conn) -> tuple[int, str]:
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "backup-admin-contract@example.com",
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day') RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


async def _seed_plain_user(conn) -> tuple[int, str]:
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "backup-plain-contract@example.com",
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day') RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def backups_dir(tmp_path_factory, monkeypatch):
    """Point the router's backup dir + sentinel at a tmp dir with two fake archives."""
    from paper_ingestion.routers import backups as backups_router

    d = tmp_path_factory.mktemp("backups")
    (d / "jarvis_20260617_120000.sql.gz").write_bytes(b"FAKE-JARVIS-DUMP")
    (d / "secrets_20260617_120000.tar.gz.enc").write_bytes(b"FAKE-ENC-SECRETS")
    monkeypatch.setattr(backups_router, "_BACKUP_DIR", d)
    monkeypatch.setattr(backups_router, "_TRIGGER_SENTINEL", d / ".backup_now")
    return d


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def admin_client(contract_conn, backups_dir):
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    _uid, cookie = await _seed_admin_user(contract_conn)
    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app, set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None}
            ),
        ):
            async with make_contract_client(app, cookie) as client:
                yield client
    finally:
        app.state.limiter.enabled = True


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def plain_client(contract_conn, backups_dir):
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    _uid, cookie = await _seed_plain_user(contract_conn)
    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app, set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None}
            ),
        ):
            async with make_contract_client(app, cookie) as client:
                yield client
    finally:
        app.state.limiter.enabled = True


async def test_list_returns_archive_metadata(admin_client):
    resp = await admin_client.get("/api/admin/backups")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {e["filename"]: e for e in body}
    assert "jarvis_20260617_120000.sql.gz" in names
    assert "secrets_20260617_120000.tar.gz.enc" in names
    jarvis = names["jarvis_20260617_120000.sql.gz"]
    assert jarvis["store"] == "jarvis"
    assert jarvis["encrypted"] is False
    assert jarvis["size_bytes"] == len(b"FAKE-JARVIS-DUMP")
    assert names["secrets_20260617_120000.tar.gz.enc"]["encrypted"] is True
    assert names["secrets_20260617_120000.tar.gz.enc"]["store"] == "secrets"


async def test_list_non_admin_gets_403(plain_client):
    resp = await plain_client.get("/api/admin/backups")
    assert resp.status_code == 403, resp.text


async def test_status_reports_last_run(admin_client):
    resp = await admin_client.get("/api/admin/backups/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backup_dir_available"] is True
    assert body["archive_count"] == 2
    assert body["last_run_at"] is not None


async def test_download_streams_known_archive(admin_client):
    resp = await admin_client.get("/api/admin/backups/jarvis_20260617_120000.sql.gz/download")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"FAKE-JARVIS-DUMP"
    assert "attachment" in resp.headers["content-disposition"]


def test_validate_name_rejects_traversal():
    """Routing-independent: the validator itself rejects a traversal name with 400.

    This is the load-bearing traversal assertion — it does not depend on how the
    ASGI server decodes %2F in the path (which can route a percent-encoded slash
    to 404 instead of reaching the handler).
    """
    from fastapi import HTTPException
    from paper_ingestion.routers import backups as backups_router

    for bad in ("../../run/secrets/jarvis_config_key", "..", "a/b", "a\\b"):
        with pytest.raises(HTTPException) as ei:
            backups_router._validate_name(bad)
        assert ei.value.status_code == 400, bad


async def test_download_rejects_traversal(admin_client):
    # Percent-encoded slashes: depending on the ASGI path-decoding, the encoded
    # name may reach _validate_name (400) or fail route matching (404). Either is
    # a safe rejection — the routing-independent guarantee is the unit test above.
    resp = await admin_client.get(
        "/api/admin/backups/..%2F..%2Frun%2Fsecrets%2Fjarvis_config_key/download"
    )
    assert resp.status_code in (400, 404), resp.text


async def test_download_rejects_unknown_shape(admin_client):
    resp = await admin_client.get("/api/admin/backups/random_file.txt/download")
    assert resp.status_code == 400, resp.text


async def test_download_missing_known_shape_404(admin_client):
    resp = await admin_client.get("/api/admin/backups/jarvis_20990101_000000.sql.gz/download")
    assert resp.status_code == 404, resp.text


async def test_trigger_writes_sentinel(admin_client, backups_dir):
    resp = await admin_client.post("/api/admin/backups")
    assert resp.status_code == 202, resp.text
    assert (backups_dir / ".backup_now").exists()


async def test_trigger_non_admin_gets_403(plain_client):
    resp = await plain_client.post("/api/admin/backups")
    assert resp.status_code == 403, resp.text
