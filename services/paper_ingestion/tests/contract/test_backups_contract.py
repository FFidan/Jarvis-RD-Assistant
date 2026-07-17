"""Contract tests for the admin Backup router (GET/POST /api/admin/backups).

Auth: routers/backups.py is included with ``dependencies=[]`` (no global
verify_api_key) and ``router.auth_exempt=True``; every route is gated by
``Depends(require_admin)`` (session role=='admin' only). We seed a real
users+sessions row so SessionMiddleware sets request.state.user_role='admin'.

The backup directory + trigger sentinel are pointed at a tmp_path via the
module-level constants so no real /backups mount is required.
"""

from __future__ import annotations

import json

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


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def restore_paths(tmp_path_factory, monkeypatch):
    """Point the restore request/status sentinels + trigger dir at a tmp dir."""
    from paper_ingestion.routers import backups as backups_router

    trig = tmp_path_factory.mktemp("restore_trigger")
    monkeypatch.setattr(backups_router, "_RESTORE_SENTINEL", trig / ".restore_request.json")
    monkeypatch.setattr(backups_router, "_RESTORE_STATUS", trig / ".restore_status.json")
    # The restore-status token file resolves from BACKUP_TRIGGER_DIR at call time
    # (jarvis_common.auth.restore_status_token_file), so point it at the tmp dir too.
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trig))
    return trig


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def restore_ready(backups_dir):
    """Complete the restore point in backups_dir by adding the missing litellm archive.

    backups_dir ships jarvis + secrets only; adding litellm makes the
    ``20260617_120000`` point ``complete`` so /restore accepts it.
    """
    (backups_dir / "litellm_20260617_120000.sql.gz").write_bytes(b"FAKE-LITELLM-DUMP")
    return "20260617_120000"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def restore_newer(backups_dir, restore_ready, tmp_path_factory, monkeypatch):
    """Make the complete restore point report compat='newer' vs the running code.

    A manifest with schema_version 99 against a code migration max of 42 forces
    the newer-than-deployment compat gate.
    """
    migrations = tmp_path_factory.mktemp("migrations")
    (migrations / "0001_init.sql").write_text("-- init")
    (migrations / "0042_feature.sql").write_text("-- feature")
    monkeypatch.setenv("DB_MIGRATIONS_DIR", str(migrations))
    ts = "20260617_120000"
    manifest = {
        "schema_version": 99,
        "app_version": "9.9.9",
        "archives": [
            {"filename": f"jarvis_{ts}.sql.gz"},
            {"filename": f"litellm_{ts}.sql.gz"},
            {"filename": "secrets_20260617_120000.tar.gz.enc"},
        ],
    }
    (backups_dir / f"manifest_{ts}.json").write_text(json.dumps(manifest))
    return ts


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def restore_corrupt_manifest(backups_dir, restore_ready):
    """Complete restore point whose manifest_<ts>.json is present but unreadable.

    Invalid JSON makes ``_read_manifest`` degrade to None while the file still
    exists on disk — the present-but-broken case the new 409 must reject.
    """
    ts = restore_ready
    (backups_dir / f"manifest_{ts}.json").write_text("{ not json")
    return ts


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def restore_no_schema_version(backups_dir, restore_ready):
    """Complete restore point with a valid manifest that lacks ``schema_version``.

    Present, parseable, archive-consistent — only the optional schema_version is
    absent (an older backup). It must still restore (no false reject).
    """
    ts = restore_ready
    manifest = {
        "app_version": "1.2.3",
        "archives": [
            {"filename": f"jarvis_{ts}.sql.gz", "sha256": "a" * 64},
            {"filename": f"litellm_{ts}.sql.gz", "sha256": "b" * 64},
            {"filename": "secrets_20260617_120000.tar.gz.enc", "sha256": "c" * 64},
        ],
    }
    (backups_dir / f"manifest_{ts}.json").write_text(json.dumps(manifest))
    return ts


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def inbox_manifest(restore_paths, monkeypatch):
    """Point _INBOX_MANIFEST at a tmp file; return a writer for the manifest list.

    The manifest is authored by ``restore.sh --inbox-manifest`` in production; here we
    write it directly so the inbox-source restore/listing paths can be exercised without
    a real /restore-inbox mount (the app never mounts it).
    """
    from paper_ingestion.routers import backups as backups_router

    path = restore_paths / ".inbox_manifest.json"
    monkeypatch.setattr(backups_router, "_INBOX_MANIFEST", path)

    def _write(entries: list[dict]) -> None:
        path.write_text(json.dumps(entries))

    return _write


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


async def test_trigger_emits_job_event(admin_client, backups_dir, monkeypatch):
    from unittest.mock import AsyncMock
    from paper_ingestion.routers import backups as backups_router

    mock_event = AsyncMock()
    monkeypatch.setattr(backups_router, "log_event", mock_event)

    resp = await admin_client.post("/api/admin/backups")
    assert resp.status_code == 202, resp.text
    mock_event.assert_awaited_once()
    assert mock_event.await_args.kwargs["category"] == "job"
    assert mock_event.await_args.kwargs["source"] == "backups"


async def test_restore_request_writes_sentinel(admin_client, restore_paths, restore_ready):
    import hashlib

    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_ready, "confirm": "RESTORE"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "scheduled"
    # A one-time restore-status bearer token is minted and returned exactly once.
    token = body["status_token"]
    assert token
    sentinel = restore_paths / ".restore_request.json"
    assert sentinel.exists()
    written = json.loads(sentinel.read_text())
    assert written["timestamp"] == "20260617_120000"
    assert written["confirm"] == "RESTORE"
    # The sentinel carries the source restore.sh keys its archive lookup on.
    assert written["source"] == "local"
    assert "requested_at" in written
    # The token HASH lives in its own file — never in the request sentinel restore.sh
    # consumes — and the raw token is never persisted.
    token_file = restore_paths / ".restore_status_token.json"
    persisted = json.loads(token_file.read_text())
    assert set(persisted) == {"sha256", "expires_at"}
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in token_file.read_text()
    assert token not in sentinel.read_text()


async def test_restore_request_emits_job_event(
    admin_client, restore_paths, restore_ready, monkeypatch
):
    from unittest.mock import AsyncMock
    from paper_ingestion.routers import backups as backups_router

    mock_event = AsyncMock()
    monkeypatch.setattr(backups_router, "log_event", mock_event)

    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_ready, "confirm": "RESTORE"},
    )
    assert resp.status_code == 202, resp.text
    mock_event.assert_awaited_once()
    assert mock_event.await_args.kwargs["category"] == "job"
    assert mock_event.await_args.kwargs["context"] == {"timestamp": restore_ready}


async def test_restore_wrong_confirm_400(admin_client, restore_paths, restore_ready):
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_ready, "confirm": "yes please"},
    )
    assert resp.status_code == 400, resp.text
    assert not (restore_paths / ".restore_request.json").exists()


async def test_restore_unknown_timestamp_404(admin_client, restore_paths, restore_ready):
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": "20990101_000000", "confirm": "RESTORE"},
    )
    assert resp.status_code == 404, resp.text
    assert not (restore_paths / ".restore_request.json").exists()


async def test_restore_incomplete_timestamp_404(admin_client, restore_paths):
    # backups_dir has jarvis + secrets but no litellm -> the point is incomplete.
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": "20260617_120000", "confirm": "RESTORE"},
    )
    assert resp.status_code == 404, resp.text


async def test_restore_newer_backup_409(admin_client, restore_paths, restore_newer):
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_newer, "confirm": "RESTORE"},
    )
    assert resp.status_code == 409, resp.text
    assert not (restore_paths / ".restore_request.json").exists()


async def test_restore_corrupt_manifest_409(admin_client, restore_paths, restore_corrupt_manifest):
    # Present-but-unreadable manifest must be rejected before any sentinel write.
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_corrupt_manifest, "confirm": "RESTORE"},
    )
    assert resp.status_code == 409, resp.text
    assert not (restore_paths / ".restore_request.json").exists()


async def test_restore_valid_manifest_without_schema_version_proceeds(
    admin_client, restore_paths, restore_no_schema_version
):
    # No false reject: a parseable manifest merely lacking schema_version still
    # restores (the 409 keys on unreadable, not on a missing schema_version field).
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_no_schema_version, "confirm": "RESTORE"},
    )
    assert resp.status_code == 202, resp.text
    assert (restore_paths / ".restore_request.json").exists()


async def test_restore_non_admin_gets_403(plain_client, restore_paths, restore_ready):
    resp = await plain_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_ready, "confirm": "RESTORE"},
    )
    assert resp.status_code == 403, resp.text


async def test_inbox_list_returns_manifest(admin_client, inbox_manifest):
    inbox_manifest(
        [{"timestamp": "20260701_030000", "complete": True, "has_secrets": True, "has_key": True}]
    )
    resp = await admin_client.get("/api/admin/backups/inbox")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"timestamp": "20260701_030000", "complete": True, "has_secrets": True, "has_key": True}
    ]


async def test_inbox_list_empty_when_absent(admin_client, restore_paths, monkeypatch):
    from paper_ingestion.routers import backups as backups_router

    monkeypatch.setattr(backups_router, "_INBOX_MANIFEST", restore_paths / ".inbox_manifest.json")
    resp = await admin_client.get("/api/admin/backups/inbox")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_inbox_list_degrades_on_malformed(admin_client, inbox_manifest, restore_paths):
    (restore_paths / ".inbox_manifest.json").write_text("{ not json")
    resp = await admin_client.get("/api/admin/backups/inbox")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_inbox_list_non_admin_gets_403(plain_client):
    resp = await plain_client.get("/api/admin/backups/inbox")
    assert resp.status_code == 403, resp.text


async def test_restore_inbox_source_writes_sentinel(admin_client, restore_paths, inbox_manifest):
    inbox_manifest(
        [{"timestamp": "20260701_030000", "complete": True, "has_secrets": True, "has_key": True}]
    )
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": "20260701_030000", "confirm": "RESTORE", "source": "inbox"},
    )
    assert resp.status_code == 202, resp.text
    written = json.loads((restore_paths / ".restore_request.json").read_text())
    # restore.sh keys its inbox archive lookup on this source field.
    assert written["source"] == "inbox"
    assert written["timestamp"] == "20260701_030000"


async def test_restore_inbox_incomplete_or_absent_404(admin_client, restore_paths, inbox_manifest):
    inbox_manifest(
        [{"timestamp": "20260701_030000", "complete": False, "has_secrets": True, "has_key": True}]
    )
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": "20260701_030000", "confirm": "RESTORE", "source": "inbox"},
    )
    assert resp.status_code == 404, resp.text
    assert not (restore_paths / ".restore_request.json").exists()

    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": "20990101_000000", "confirm": "RESTORE", "source": "inbox"},
    )
    assert resp.status_code == 404, resp.text
    assert not (restore_paths / ".restore_request.json").exists()


async def test_restore_inbox_missing_key_409(admin_client, restore_paths, inbox_manifest):
    inbox_manifest(
        [{"timestamp": "20260701_030000", "complete": True, "has_secrets": True, "has_key": False}]
    )
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": "20260701_030000", "confirm": "RESTORE", "source": "inbox"},
    )
    assert resp.status_code == 409, resp.text
    assert "operator key" in resp.json()["detail"].lower()
    assert not (restore_paths / ".restore_request.json").exists()


async def test_restore_inbox_missing_secrets_409(admin_client, restore_paths, inbox_manifest):
    # A secrets-less off-host set would swap both DBs then fail post-swap: the
    # validator must reject it up front with a 409 and never write the restore sentinel,
    # even when the point is otherwise complete and keyed.
    inbox_manifest(
        [{"timestamp": "20260701_030000", "complete": True, "has_secrets": False, "has_key": True}]
    )
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": "20260701_030000", "confirm": "RESTORE", "source": "inbox"},
    )
    assert resp.status_code == 409, resp.text
    assert "secrets" in resp.json()["detail"].lower()
    assert not (restore_paths / ".restore_request.json").exists()


async def test_restore_status_idle_when_absent(admin_client, restore_paths):
    resp = await admin_client.get("/api/admin/backups/restore/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "idle"
    assert body["current_step"] is None
    assert body["steps"] == []


async def test_restore_status_pending_when_request_queued(admin_client, restore_paths):
    # A queued restore (the request sentinel exists) with no status file yet must
    # report "pending" so the UI keeps tracking through the few-second window
    # before the sidecar writes the first status (not "idle" -> stop).
    (restore_paths / ".restore_request.json").write_text(
        '{"timestamp": "20260617_120000", "confirm": "RESTORE"}'
    )
    resp = await admin_client.get("/api/admin/backups/restore/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "pending"


async def test_restore_status_pending_overrides_stale_status_file(admin_client, restore_paths):
    # A leftover status file from a prior run must not mask a freshly-queued
    # restore: the sentinel's presence wins -> "pending".
    (restore_paths / ".restore_status.json").write_text('{"state": "done", "steps": []}')
    (restore_paths / ".restore_request.json").write_text(
        '{"timestamp": "20260617_120000", "confirm": "RESTORE"}'
    )
    resp = await admin_client.get("/api/admin/backups/restore/status")
    assert resp.json()["state"] == "pending"


async def test_restore_status_reflects_status_file(admin_client, restore_paths):
    (restore_paths / ".restore_status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "current_step": "Restoring database",
                "steps": [
                    {"name": "Safety backup", "status": "done"},
                    {"name": "Restoring database", "status": "running"},
                    {"name": "Restoring API-key store", "status": "pending"},
                    {"name": "Restoring search index", "status": "pending"},
                    {"name": "Finishing up", "status": "pending"},
                ],
                "safety_backup_ts": "20260617_115900",
                "started_at": "2026-06-17T12:00:00+00:00",
                "finished_at": None,
                "error": None,
                "drop_started": True,
            }
        )
    )
    resp = await admin_client.get("/api/admin/backups/restore/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "running"
    assert body["current_step"] == "Restoring database"
    assert len(body["steps"]) == 5
    assert body["steps"][1] == {"name": "Restoring database", "status": "running"}
    assert body["safety_backup_ts"] == "20260617_115900"
    # The sidecar's extra drop_started key is dropped by the response model.
    assert "drop_started" not in body


async def test_restore_status_malformed_degrades_to_idle(admin_client, restore_paths):
    (restore_paths / ".restore_status.json").write_text("{ not valid json")
    resp = await admin_client.get("/api/admin/backups/restore/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "idle"


async def test_restore_status_non_admin_gets_403(plain_client, restore_paths):
    resp = await plain_client.get("/api/admin/backups/restore/status")
    assert resp.status_code == 403, resp.text


async def test_restore_duplicate_request_409(
    admin_client, restore_paths, restore_ready, monkeypatch
):
    from unittest.mock import AsyncMock
    from paper_ingestion.routers import backups as backups_router

    mock_audit = AsyncMock()
    monkeypatch.setattr(backups_router, "log_audit", mock_audit)

    # Plant a pre-existing sentinel to simulate a pending restore.
    sentinel = restore_paths / ".restore_request.json"
    sentinel.write_text(
        '{"timestamp": "20260617_120000", "confirm": "RESTORE", "requested_at": "2026-01-01T00:00:00+00:00"}'
    )
    resp = await admin_client.post(
        "/api/admin/backups/restore",
        json={"timestamp": restore_ready, "confirm": "RESTORE"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"].lower()
    assert "pending" in detail or "running" in detail
    # Duplicate-rejected path must never write an audit row.
    mock_audit.assert_not_called()
    # The sentinel must not have been overwritten.
    written = sentinel.read_text()
    assert "20260617_120000" in written


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def delete_paths(tmp_path_factory, monkeypatch):
    """Point the delete-request sentinel + retention config at a tmp trigger dir."""
    from paper_ingestion.routers import backups as backups_router

    trig = tmp_path_factory.mktemp("delete_trigger")
    monkeypatch.setattr(backups_router, "_DELETE_SENTINEL", trig / ".delete_request.json")
    monkeypatch.setattr(backups_router, "_RETENTION_CONFIG", trig / ".retention.json")
    return trig


async def test_delete_writes_sentinel(admin_client, restore_paths, delete_paths, restore_ready):
    # Verified: paper_ingestion/routers/backups.py:657 request_delete_restore_point
    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"status": "scheduled"}
    sentinel = delete_paths / ".delete_request.json"
    assert sentinel.exists()
    written = json.loads(sentinel.read_text())
    # Byte-shape the sidecar's prune.sh parses: a timestamps list, the confirm
    # token, and version 1. Assert the whole request shape at once.
    assert {k: written[k] for k in ("timestamps", "confirm", "version")} == {
        "timestamps": ["20260617_120000"],
        "confirm": "DELETE",
        "version": 1,
    }
    assert "requested_at" in written


async def test_delete_emits_job_event(
    admin_client, restore_paths, delete_paths, restore_ready, monkeypatch
):
    from unittest.mock import AsyncMock
    from paper_ingestion.routers import backups as backups_router

    mock_event = AsyncMock()
    monkeypatch.setattr(backups_router, "log_event", mock_event)

    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 202, resp.text
    mock_event.assert_awaited_once()
    assert mock_event.await_args.kwargs["category"] == "job"
    assert mock_event.await_args.kwargs["context"] == {"timestamp": restore_ready}


async def test_delete_never_writes_under_backup_dir(
    admin_client, restore_paths, delete_paths, restore_ready, backups_dir
):
    # The delete endpoint writes ONLY into the trigger volume; /backups is read-only.
    before = {p.name for p in backups_dir.iterdir()}
    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 202, resp.text
    # No sentinel (or any new file) landed under _BACKUP_DIR, and no archive was removed.
    assert not (backups_dir / ".delete_request.json").exists()
    assert {p.name for p in backups_dir.iterdir()} == before


async def test_delete_wrong_confirm_400(admin_client, restore_paths, delete_paths, restore_ready):
    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "yes please"},
    )
    assert resp.status_code == 400, resp.text
    assert not (delete_paths / ".delete_request.json").exists()


async def test_delete_unknown_timestamp_404(admin_client, restore_paths, delete_paths):
    resp = await admin_client.post(
        "/api/admin/backups/restore-points/20990101_000000/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 404, resp.text
    assert not (delete_paths / ".delete_request.json").exists()


async def test_delete_in_flight_restore_409(
    admin_client, restore_paths, delete_paths, restore_ready
):
    # Seed a restore whose target IS this timestamp -> deleting it must be refused.
    (restore_paths / ".restore_request.json").write_text(
        '{"timestamp": "20260617_120000", "confirm": "RESTORE"}'
    )
    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 409, resp.text
    assert "restore" in resp.json()["detail"].lower()
    assert not (delete_paths / ".delete_request.json").exists()


async def test_delete_in_flight_safety_backup_409(
    admin_client, restore_paths, delete_paths, backups_dir
):
    # A restore's just-taken safety backup is also protected (matches prune.sh).
    ts = "20260101_090000"
    (backups_dir / f"jarvis_{ts}.sql.gz").write_bytes(b"X")
    (restore_paths / ".restore_status.json").write_text(
        f'{{"state": "running", "safety_backup_ts": "{ts}"}}'
    )
    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{ts}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 409, resp.text
    assert not (delete_paths / ".delete_request.json").exists()


async def test_delete_audits_before_writing_sentinel(
    admin_client, restore_paths, delete_paths, restore_ready, monkeypatch
):
    from unittest.mock import AsyncMock
    from paper_ingestion.routers import backups as backups_router

    sentinel = delete_paths / ".delete_request.json"

    async def _audit_before_write(*_args, **_kwargs):
        # The audit row must be written while the sentinel does NOT yet exist, so a
        # failed audit 500s without ever queuing a delete.
        assert not sentinel.exists()

    mock_audit = AsyncMock(side_effect=_audit_before_write)
    monkeypatch.setattr(backups_router, "log_audit", mock_audit)

    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 202, resp.text
    assert sentinel.exists()
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["action"] == "backup.delete_requested"
    assert mock_audit.call_args.kwargs["resource"] == f"backups/{restore_ready}"


async def test_delete_duplicate_pending_409(
    admin_client, restore_paths, delete_paths, restore_ready, monkeypatch
):
    from unittest.mock import AsyncMock
    from paper_ingestion.routers import backups as backups_router

    mock_audit = AsyncMock()
    monkeypatch.setattr(backups_router, "log_audit", mock_audit)

    # A delete is already pending -> the second request is a no-op (no audit row).
    sentinel = delete_paths / ".delete_request.json"
    sentinel.write_text('{"timestamps": ["20260615_000000"], "confirm": "DELETE", "version": 1}')
    resp = await admin_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 409, resp.text
    assert "pending" in resp.json()["detail"].lower()
    mock_audit.assert_not_called()
    # The pending sentinel must not have been overwritten.
    assert "20260615_000000" in sentinel.read_text()


async def test_delete_non_admin_gets_403(plain_client, restore_paths, delete_paths, restore_ready):
    resp = await plain_client.post(
        f"/api/admin/backups/restore-points/{restore_ready}/delete",
        json={"confirm": "DELETE"},
    )
    assert resp.status_code == 403, resp.text


async def test_retention_get_defaults_when_absent(admin_client, delete_paths):
    resp = await admin_client.get("/api/admin/backups/retention")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"keep_last_n": None, "max_age_days": None}


async def test_retention_put_persists_and_get_reads_back(admin_client, delete_paths):
    resp = await admin_client.put(
        "/api/admin/backups/retention",
        json={"keep_last_n": 5, "max_age_days": 30},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"keep_last_n": 5, "max_age_days": 30}
    # Persisted to the file the bash sidecar greps (integers, not the DB).
    on_disk = json.loads((delete_paths / ".retention.json").read_text())
    assert on_disk == {"keep_last_n": 5, "max_age_days": 30}
    got = await admin_client.get("/api/admin/backups/retention")
    assert got.json() == {"keep_last_n": 5, "max_age_days": 30}


async def test_retention_put_emits_config_event(admin_client, delete_paths, monkeypatch):
    from unittest.mock import AsyncMock
    from paper_ingestion.routers import backups as backups_router

    mock_event = AsyncMock()
    monkeypatch.setattr(backups_router, "log_event", mock_event)

    resp = await admin_client.put(
        "/api/admin/backups/retention",
        json={"keep_last_n": 5, "max_age_days": 30},
    )
    assert resp.status_code == 200, resp.text
    mock_event.assert_awaited_once()
    assert mock_event.await_args.kwargs["category"] == "config"
    assert mock_event.await_args.kwargs["source"] == "backups"


async def test_retention_put_accepts_null(admin_client, delete_paths):
    resp = await admin_client.put(
        "/api/admin/backups/retention",
        json={"keep_last_n": None, "max_age_days": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"keep_last_n": None, "max_age_days": None}


async def test_retention_put_rejects_negative(admin_client, delete_paths):
    resp = await admin_client.put(
        "/api/admin/backups/retention",
        json={"keep_last_n": -1, "max_age_days": 30},
    )
    assert resp.status_code == 422, resp.text
    assert not (delete_paths / ".retention.json").exists()


async def test_retention_non_admin_gets_403(plain_client, delete_paths):
    resp = await plain_client.get("/api/admin/backups/retention")
    assert resp.status_code == 403, resp.text
