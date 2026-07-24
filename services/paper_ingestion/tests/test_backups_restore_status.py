"""Restore status, recovery-session, quarantine, and acknowledgement tests.

Covers missing status fields, database-independent token polling, exact restore
binding, linked or malformed state, concurrent requests, configured-owner
authentication, and acknowledgement replay refusal.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport
from jarvis_common.auth import (
    RESTORE_ACKNOWLEDGE_PATH,
    RESTORE_STATUS_PATH,
    restore_status_bearer_valid,
    restore_status_token_file,
    verify_api_key,
)
from jarvis_common.testing_contract_apps import configure_contract_api_key

from paper_ingestion.routers import backups as bk
from paper_ingestion.routers.backups import RestoreStatus

_STATUS_PATH = "/api/admin/backups/restore/status"
_ACKNOWLEDGE_PATH = "/api/admin/backups/restore/acknowledge"
_RESTORE_ID = "0123456789abcdef0123456789abcdef"
_REQUESTED_AT = "2026-07-21T20:00:00+00:00"
_COMPLETED_AT = "2026-07-21T20:05:00+00:00"


class _DeadPool:
    """Raise on database access to verify database-independent request paths."""

    def __getattr__(self, name: str):  # noqa: ANN204
        raise AssertionError(f"restore_status accessed the database pool ({name})")


def _seed_complete_point(d, ts: str) -> None:
    """Write a complete restore point (databases + PDFs) under ``d``."""
    for name in (f"jarvis_{ts}.sql.gz", f"litellm_{ts}.sql.gz", f"pdfs_{ts}.tar.gz"):
        (d / name).write_bytes(b"x" * 8)


def _quarantine_payload(*, restore_id: str = _RESTORE_ID) -> dict[str, object]:
    return {
        "version": 1,
        "restore_id": restore_id,
        "source": "inbox",
        "requested_at": _REQUESTED_AT,
        "completed_at": _COMPLETED_AT,
        "review_state": "awaiting_review",
    }


def _seed_quarantine(trigger, *, restore_id: str = _RESTORE_ID):
    quarantine = trigger / ".outbound-quarantine.json"
    quarantine.write_text(json.dumps(_quarantine_payload(restore_id=restore_id)))
    return quarantine


def _unit_request(*, headers: dict[str, str] | None = None, user_id: int | None = None):
    state = SimpleNamespace(user_id=user_id) if user_id is not None else SimpleNamespace()
    return SimpleNamespace(
        headers=headers or {},
        state=state,
        app=SimpleNamespace(state=SimpleNamespace(db_pool=SimpleNamespace())),
        url=SimpleNamespace(path=_ACKNOWLEDGE_PATH),
        method="POST",
        client=None,
    )


def test_manual_step_fields_round_trip() -> None:
    status = RestoreStatus.model_validate(
        {
            "state": "done",
            "current_step": None,
            "steps": [{"name": "Finishing up", "status": "done"}],
            "safety_backup_ts": "20260708_120000",
            "started_at": "2026-07-08T12:00:00+00:00",
            "finished_at": "2026-07-08T12:05:00+00:00",
            "error": "held in maintenance",
            "manual_steps_required": True,
            "phase": "maintenance-held",
        }
    )
    assert status.manual_steps_required is True
    assert status.phase == "maintenance-held"


def test_unknown_extra_keys_are_ignored() -> None:
    status = RestoreStatus.model_validate({"state": "running", "drop_started": True})
    assert status.state == "running"
    assert status.manual_steps_required is False
    assert status.phase is None


def test_legacy_status_without_new_fields_defaults() -> None:
    status = RestoreStatus.model_validate(
        {
            "state": "done",
            "current_step": None,
            "steps": [],
            "safety_backup_ts": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
    )
    assert status.manual_steps_required is False
    assert status.phase is None
    assert status.restore_id is None
    assert status.source is None
    assert status.quarantine == "none"


def test_write_status_token_binds_the_current_restore(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(tmp_path))
    token = "correct-horse-battery-staple"
    restore_id = "0123456789abcdef0123456789abcdef"
    requested_at = datetime.now(UTC).isoformat()

    assert (
        bk._write_status_token(
            token,
            restore_id=restore_id,
            source="inbox",
            requested_at=requested_at,
        )
        is True
    )

    f = restore_status_token_file()
    persisted = json.loads(f.read_text())
    assert set(persisted) == {
        "version",
        "sha256",
        "expires_at",
        "restore_id",
        "source",
        "requested_at",
    }
    assert persisted["version"] == 2
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert persisted["restore_id"] == restore_id
    assert persisted["source"] == "inbox"
    assert persisted["requested_at"] == requested_at
    assert token not in f.read_text()
    assert (f.stat().st_mode & 0o777) == 0o600
    expires = datetime.fromisoformat(persisted["expires_at"])
    assert timedelta(hours=1, minutes=55) < expires - datetime.now(UTC) <= timedelta(hours=2)


@pytest.mark.asyncio
async def test_request_restore_returns_token_and_writes_only_hash(tmp_path, monkeypatch) -> None:
    from jarvis_common.auth import require_admin, verify_api_key

    from paper_ingestion.main import app

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    ts = "20260708_120000"
    _seed_complete_point(backup_dir, ts)

    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setattr(bk, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "log_audit", AsyncMock())
    monkeypatch.setattr(bk, "log_event", AsyncMock())
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)
    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/admin/backups/restore",
                json={"timestamp": ts, "confirm": "RESTORE"},
            )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 202, resp.text
    response = resp.json()
    token = response["status_token"]
    restore_id = response["restore_id"]
    assert token
    assert len(restore_id) == 32
    f = restore_status_token_file()
    persisted = json.loads(f.read_text())
    assert persisted["restore_id"] == restore_id
    assert persisted["source"] == "local"
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert datetime.fromisoformat(response["expires_at"]) == datetime.fromisoformat(
        persisted["expires_at"]
    )
    assert token not in f.read_text()
    # The token record is stored separately from the restore request.
    assert (trigger / ".restore_request.json").exists()
    request = json.loads((trigger / ".restore_request.json").read_text())
    assert request["restore_id"] == restore_id
    assert request["requested_at"] == persisted["requested_at"]
    assert request["allow_missing_pdfs"] is False
    assert f.name == ".restore_status_token.json"


@pytest.mark.asyncio
async def test_request_restore_refuses_when_token_record_cannot_be_persisted(
    tmp_path, monkeypatch
) -> None:
    from jarvis_common.auth import require_admin, verify_api_key

    from paper_ingestion.main import app

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    ts = "20260708_120000"
    _seed_complete_point(backup_dir, ts)

    monkeypatch.setattr(bk, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "_write_status_token", lambda *args, **kwargs: False)
    monkeypatch.setattr(bk, "log_audit", AsyncMock())
    monkeypatch.setattr(bk, "log_event", AsyncMock())
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)
    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/admin/backups/restore",
                json={"timestamp": ts, "confirm": "RESTORE"},
            )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 503, resp.text
    assert not (trigger / ".restore_request.json").exists()


@pytest.mark.asyncio
async def test_request_restore_forwards_legacy_missing_pdf_authorization(
    tmp_path, monkeypatch
) -> None:
    from jarvis_common.auth import require_admin, verify_api_key

    from paper_ingestion.main import app

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    ts = "20260708_120000"
    names = [
        f"jarvis_{ts}.sql.gz.enc",
        f"litellm_{ts}.sql.gz.enc",
        f"secrets_{ts}.tar.gz.enc",
    ]
    for name in names:
        (backup_dir / name).write_bytes(b"legacy")
    (backup_dir / f"manifest_{ts}.json").write_text(
        json.dumps(
            {
                "timestamp": ts,
                "app_version": "1.1.3",
                "schema_version": 100,
                "archives": [{"filename": name} for name in names],
            }
        )
    )
    (backup_dir / f"manifest_{ts}.json.hmac").write_text("0" * 64)

    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setattr(bk, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "log_audit", AsyncMock())
    monkeypatch.setattr(bk, "log_event", AsyncMock())
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)
    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            refused = await client.post(
                "/api/admin/backups/restore",
                json={"timestamp": ts, "confirm": "RESTORE"},
            )
            accepted = await client.post(
                "/api/admin/backups/restore",
                json={
                    "timestamp": ts,
                    "confirm": "RESTORE",
                    "allow_missing_pdfs": True,
                },
            )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert refused.status_code == 409, refused.text
    assert accepted.status_code == 202, accepted.text
    request = json.loads((trigger / ".restore_request.json").read_text())
    assert request["allow_missing_pdfs"] is True


@pytest.mark.asyncio
async def test_restore_status_bearer_token_is_db_free(tmp_path, monkeypatch) -> None:
    """A valid restore token authorizes polling without database access."""
    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "_RESTORE_STATUS", trigger / ".restore_status.json")

    token = "db-free-poll-token-value"
    assert (
        bk._write_status_token(
            token,
            restore_id="0123456789abcdef0123456789abcdef",
            source="inbox",
            requested_at=datetime.now(UTC).isoformat(),
        )
        is True
    )
    (trigger / ".restore_status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "current_step": "Restoring database",
                "steps": [{"name": "Restoring database", "status": "running"}],
            }
        )
    )

    monkeypatch.setattr(app.state, "db_pool", _DeadPool(), raising=False)
    app.state.limiter.enabled = False
    # Configure an API key so the restore token is required for this request.
    with configure_contract_api_key(monkeypatch):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(_STATUS_PATH, headers={"Authorization": f"Bearer {token}"})
        finally:
            app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "running"
    assert body["current_step"] == "Restoring database"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["wrong", "expired", "absent"])
async def test_restore_status_bad_bearer_is_rejected(tmp_path, monkeypatch, scenario) -> None:
    """Reject a wrong, expired, or absent restore token."""
    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "_RESTORE_STATUS", trigger / ".restore_status.json")

    if scenario == "wrong":
        assert (
            bk._write_status_token(
                "the-real-token",
                restore_id="0123456789abcdef0123456789abcdef",
                source="inbox",
                requested_at=datetime.now(UTC).isoformat(),
            )
            is True
        )
        header = {"Authorization": "Bearer WRONG"}
    elif scenario == "expired":
        (trigger / ".restore_status_token.json").write_text(
            json.dumps(
                {
                    "sha256": hashlib.sha256(b"tok").hexdigest(),
                    "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                }
            )
        )
        header = {"Authorization": "Bearer tok"}
    else:  # absent token file
        header = {"Authorization": "Bearer anything"}

    # Rejection remains database-independent when no audit pool is available.
    monkeypatch.setattr(app.state, "db_pool", None, raising=False)
    app.state.limiter.enabled = False
    with configure_contract_api_key(monkeypatch):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(_STATUS_PATH, headers=header)
        finally:
            app.state.limiter.enabled = True

    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_restore_status_api_key_path_still_works(tmp_path, monkeypatch) -> None:
    """The existing ops X-API-Key path reaches the endpoint and returns progress."""
    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "_RESTORE_STATUS", trigger / ".restore_status.json")
    (trigger / ".restore_status.json").write_text(json.dumps({"state": "idle"}))

    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)
    app.state.limiter.enabled = False
    with configure_contract_api_key(monkeypatch) as key:
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(_STATUS_PATH, headers={"X-API-Key": key})
        finally:
            app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "idle"


@pytest.mark.parametrize(
    "content",
    [
        "[1, 2, 3]",  # valid JSON, but not an object -> no .get
        '"a string"',
        "123",
        "true",
        "null",
        "not json at all {",  # invalid JSON
        # well-formed object but a NAIVE expiry -> aware/naive compare would raise
        '{"sha256": "deadbeef", "expires_at": "2099-01-01T00:00:00"}',
    ],
)
def test_bearer_valid_never_raises_on_malformed_token_file(tmp_path, monkeypatch, content) -> None:
    """Treat a malformed token record as invalid without raising."""
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(tmp_path))
    (tmp_path / ".restore_status_token.json").write_text(content)
    request = SimpleNamespace(headers={"Authorization": "Bearer anything"})

    assert restore_status_bearer_valid(request) is False


def test_bearer_rejects_an_unbound_legacy_token_file(tmp_path, monkeypatch) -> None:
    token = "legacy-unbound-token"
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(tmp_path))
    (tmp_path / ".restore_status_token.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        )
    )
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"})

    assert restore_status_bearer_valid(request) is False


# ---------------------------------------------------------------------------
# Restore acknowledgement authentication, token consumption, and owner fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_bearer_is_bound_to_exact_method_and_path(tmp_path, monkeypatch) -> None:
    token = "current-inbox-recovery-token"
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(tmp_path))
    assert bk._write_status_token(
        token,
        restore_id=_RESTORE_ID,
        source="inbox",
        requested_at=_REQUESTED_AT,
    )

    with configure_contract_api_key(monkeypatch):
        for method, path in (("GET", RESTORE_STATUS_PATH), ("POST", RESTORE_ACKNOWLEDGE_PATH)):
            request = _unit_request(headers={"Authorization": f"Bearer {token}"})
            request.method = method
            request.url.path = path
            await verify_api_key(request, None)

        for method, path in (
            ("POST", RESTORE_STATUS_PATH),
            ("GET", RESTORE_ACKNOWLEDGE_PATH),
            ("POST", f"{RESTORE_ACKNOWLEDGE_PATH}/"),
        ):
            request = _unit_request(headers={"Authorization": f"Bearer {token}"})
            request.method = method
            request.url.path = path
            with pytest.raises(HTTPException) as exc:
                await verify_api_key(request, None)
            assert exc.value.status_code == 403

        # A same-host token remains valid for status polling but never opens the
        # off-host acknowledgement exception.
        assert bk._write_status_token(
            token,
            restore_id=_RESTORE_ID,
            source="local",
            requested_at=_REQUESTED_AT,
        )
        request = _unit_request(headers={"Authorization": f"Bearer {token}"})
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(request, None)
        assert exc.value.status_code == 403


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_restore_bearer_rejects_linked_token_state(tmp_path, monkeypatch, link_kind) -> None:
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(tmp_path))
    token = "linked-restore-token"
    real = tmp_path / "real-token.json"
    real.write_text(
        json.dumps(
            {
                "version": 2,
                "sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "restore_id": _RESTORE_ID,
                "source": "inbox",
                "requested_at": _REQUESTED_AT,
            }
        )
    )
    token_record = restore_status_token_file()
    if link_kind == "symlink":
        token_record.symlink_to(real)
    else:
        os.link(real, token_record)

    request = _unit_request(headers={"Authorization": f"Bearer {token}"})
    assert restore_status_bearer_valid(request) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quarantine_kind", "expected_status"),
    [("valid", 503), ("malformed", 503), ("dangling_symlink", 503)],
)
async def test_request_restore_refuses_outstanding_quarantine_before_audit(
    tmp_path, monkeypatch, quarantine_kind, expected_status
) -> None:
    from jarvis_common.auth import require_admin

    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    timestamp = "20260721_200000"
    _seed_complete_point(backup_dir, timestamp)
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(trigger / ".outbound-quarantine.json"))
    monkeypatch.setattr(bk, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    audit = AsyncMock()
    monkeypatch.setattr(bk, "log_audit", audit)
    monkeypatch.setattr(bk, "log_event", AsyncMock())
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)

    quarantine = trigger / ".outbound-quarantine.json"
    if quarantine_kind == "valid":
        _seed_quarantine(trigger)
    elif quarantine_kind == "malformed":
        quarantine.write_text("not json")
    else:
        quarantine.symlink_to(trigger / "missing-target")

    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/admin/backups/restore",
                json={"timestamp": timestamp, "confirm": "RESTORE"},
            )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert response.status_code == expected_status, response.text
    audit.assert_not_awaited()
    assert not os.path.lexists(trigger / ".restore_request.json")
    assert not os.path.lexists(trigger / ".restore_status_token.json")


@pytest.mark.asyncio
async def test_request_restore_refuses_an_active_lifecycle_operation_before_audit(
    tmp_path, monkeypatch
) -> None:
    from jarvis_common.auth import require_admin

    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    backup_dir = tmp_path / "backups"
    lifecycle_dir = backup_dir / ".lifecycle"
    lifecycle_dir.mkdir(parents=True)
    (lifecycle_dir / "operation.state").write_text("restore\n")
    timestamp = "20260721_200000"
    _seed_complete_point(backup_dir, timestamp)
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(trigger / ".outbound-quarantine.json"))
    monkeypatch.setattr(bk, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    audit = AsyncMock()
    monkeypatch.setattr(bk, "log_audit", audit)
    monkeypatch.setattr(bk, "log_event", AsyncMock())
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)

    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/admin/backups/restore",
                json={"timestamp": timestamp, "confirm": "RESTORE"},
            )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert response.status_code == 409, response.text
    assert "lifecycle operation is already active" in response.json()["detail"]
    audit.assert_not_awaited()
    assert not os.path.lexists(trigger / ".restore_request.json")
    assert not os.path.lexists(trigger / ".restore_status_token.json")


@pytest.mark.asyncio
async def test_concurrent_restore_requests_keep_the_winners_token_record(
    tmp_path, monkeypatch
) -> None:
    from jarvis_common.auth import require_admin

    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    timestamp = "20260721_200000"
    _seed_complete_point(backup_dir, timestamp)
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(trigger / ".outbound-quarantine.json"))
    monkeypatch.setattr(bk, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")

    both_audited = asyncio.Event()
    audit_count = 0

    async def synchronized_audit(*args, **kwargs) -> None:
        nonlocal audit_count
        audit_count += 1
        if audit_count == 2:
            both_audited.set()
        await both_audited.wait()

    monkeypatch.setattr(bk, "log_audit", synchronized_audit)
    monkeypatch.setattr(bk, "log_event", AsyncMock())
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)
    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                client.post(
                    "/api/admin/backups/restore",
                    json={"timestamp": timestamp, "confirm": "RESTORE"},
                ),
                client.post(
                    "/api/admin/backups/restore",
                    json={"timestamp": timestamp, "confirm": "RESTORE"},
                ),
            )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    accepted = [response for response in responses if response.status_code == 202]
    refused = [response for response in responses if response.status_code == 409]
    assert len(accepted) == 1
    assert len(refused) == 1
    sentinel = json.loads((trigger / ".restore_request.json").read_text())
    token_record = json.loads(restore_status_token_file().read_text())
    assert sentinel["restore_id"] == accepted[0].json()["restore_id"]
    assert token_record["restore_id"] == accepted[0].json()["restore_id"]
    assert sentinel["requested_at"] == token_record["requested_at"]


def _acknowledgement(restore_id: str = _RESTORE_ID):
    return bk.RestoreAcknowledgement(
        restore_id=restore_id,
        source="inbox",
        confirm=bk.RESTORE_ACKNOWLEDGEMENT_PHRASE,
    )


@pytest.mark.asyncio
async def test_current_bearer_atomically_acknowledges_and_replay_fails(
    tmp_path, monkeypatch
) -> None:
    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    quarantine = _seed_quarantine(trigger)
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    token = "current-acknowledgement-token"
    assert bk._write_status_token(
        token,
        restore_id=_RESTORE_ID,
        source="inbox",
        requested_at=_REQUESTED_AT,
    )
    monkeypatch.setattr(app.state, "db_pool", _DeadPool(), raising=False)
    app.state.limiter.enabled = False
    with configure_contract_api_key(monkeypatch):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    _ACKNOWLEDGE_PATH,
                    headers={"Authorization": f"Bearer {token}"},
                    json=_acknowledgement().model_dump(),
                )
                replay = await client.post(
                    _ACKNOWLEDGE_PATH,
                    headers={"Authorization": f"Bearer {token}"},
                    json=_acknowledgement().model_dump(),
                )
        finally:
            app.state.limiter.enabled = True

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "acknowledged", "restore_id": _RESTORE_ID}
    assert not os.path.lexists(quarantine)
    assert not os.path.lexists(restore_status_token_file())
    assert replay.status_code in (401, 403), replay.text


@pytest.mark.asyncio
async def test_acknowledgement_failure_after_token_consume_keeps_quarantine(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    quarantine = _seed_quarantine(trigger)
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    token = "consume-before-commit-token"
    assert bk._write_status_token(
        token,
        restore_id=_RESTORE_ID,
        source="inbox",
        requested_at=_REQUESTED_AT,
    )

    original_unlink = Path.unlink

    def fail_quarantine_remove(path, *args, **kwargs) -> None:
        if path == quarantine:
            raise OSError("simulated quarantine unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_quarantine_remove)
    monkeypatch.setattr(app.state, "db_pool", _DeadPool(), raising=False)
    app.state.limiter.enabled = False
    with configure_contract_api_key(monkeypatch):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    _ACKNOWLEDGE_PATH,
                    headers={"Authorization": f"Bearer {token}"},
                    json=_acknowledgement().model_dump(),
                )
        finally:
            app.state.limiter.enabled = True

    assert response.status_code == 503, response.text
    assert os.path.lexists(quarantine)
    assert not os.path.lexists(restore_status_token_file())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["wrong_token", "wrong_id", "local_source", "expired", "malformed", "prior_restore"],
)
async def test_bearer_acknowledgement_rejects_unbound_or_invalid_state(
    tmp_path, monkeypatch, scenario
) -> None:
    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    quarantine = _seed_quarantine(trigger)
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    token = "bound-token"
    source = "local" if scenario == "local_source" else "inbox"
    requested_at = "2026-07-20T20:00:00+00:00" if scenario == "prior_restore" else _REQUESTED_AT
    assert bk._write_status_token(
        token,
        restore_id=_RESTORE_ID,
        source=source,
        requested_at=requested_at,
    )
    token_record = restore_status_token_file()
    if scenario == "expired":
        data = json.loads(token_record.read_text())
        data["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        token_record.write_text(json.dumps(data))
    elif scenario == "malformed":
        token_record.write_text("not json")

    presented = "wrong-token" if scenario == "wrong_token" else token
    restore_id = "fedcba9876543210fedcba9876543210" if scenario == "wrong_id" else _RESTORE_ID
    monkeypatch.setattr(app.state, "db_pool", _DeadPool(), raising=False)
    app.state.limiter.enabled = False
    with configure_contract_api_key(monkeypatch):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    _ACKNOWLEDGE_PATH,
                    headers={"Authorization": f"Bearer {presented}"},
                    json=_acknowledgement(restore_id).model_dump(),
                )
        finally:
            app.state.limiter.enabled = True

    assert response.status_code in (401, 403, 409), response.text
    assert os.path.lexists(quarantine)
    assert os.path.lexists(token_record)


# ---------------------------------------------------------------------------
# Off-host upload grant used by the restore uploader.
# ---------------------------------------------------------------------------


def test_write_upload_grant_persists_only_hash_0644(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bk, "_UPLOAD_GRANT", tmp_path / ".upload_grant.json")
    token = "upload-grant-token-value"

    assert bk._write_upload_grant(token) is True

    f = bk._UPLOAD_GRANT
    persisted = json.loads(f.read_text())
    # Server storage contains the hash and expiry, not the raw token.
    assert set(persisted) == {"sha256", "expires_at"}
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in f.read_text()
    # The uploader reads this non-secret hash record through its read-only mount.
    assert (f.stat().st_mode & 0o777) == 0o644
    expires = datetime.fromisoformat(persisted["expires_at"])
    assert timedelta(minutes=25) < expires - datetime.now(UTC) <= timedelta(minutes=30)


def test_write_upload_grant_replaces_preexisting_symlink(tmp_path, monkeypatch) -> None:
    # A pre-existing symlink at the grant path must be replaced, not followed:
    # _atomic_write_private_json stages a random O_EXCL|O_NOFOLLOW temporary and
    # os.replaces it over the destination, so the symlink's referent is left
    # untouched and the grant path becomes a regular file holding the record.
    grant = tmp_path / ".upload_grant.json"
    referent = tmp_path / "referent.txt"
    referent.write_text("original-referent-content")
    grant.symlink_to(referent)
    monkeypatch.setattr(bk, "_UPLOAD_GRANT", grant)
    token = "upload-grant-token-value"

    assert bk._write_upload_grant(token) is True

    # The grant path is now a regular file, not the symlink it was.
    assert os.path.islink(grant) is False
    assert grant.is_file()
    # The symlink's former referent is untouched — the write never followed it.
    assert referent.read_text() == "original-referent-content"
    persisted = json.loads(grant.read_text())
    assert set(persisted) == {"sha256", "expires_at"}
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert (grant.stat().st_mode & 0o777) == 0o644


def test_write_upload_grant_forces_0644_under_restrictive_umask(tmp_path, monkeypatch) -> None:
    # os.open's mode is umask-subject; the hardened writer fchmods the exact
    # bits, so a hardened host umask cannot narrow the grant below the 0o644 the
    # co-mounted uploader needs to read through its read-only mount.
    grant = tmp_path / ".upload_grant.json"
    monkeypatch.setattr(bk, "_UPLOAD_GRANT", grant)
    old_umask = os.umask(0o077)
    try:
        assert bk._write_upload_grant("upload-grant-token-value") is True
    finally:
        os.umask(old_umask)
    assert (grant.stat().st_mode & 0o777) == 0o644


@pytest.mark.asyncio
async def test_upload_grant_endpoint_returns_token_once_and_audits(tmp_path, monkeypatch) -> None:
    from jarvis_common.auth import require_admin, verify_api_key

    from paper_ingestion.main import app

    grant_file = tmp_path / ".upload_grant.json"
    monkeypatch.setattr(bk, "_UPLOAD_GRANT", grant_file)
    audit = AsyncMock()
    monkeypatch.setattr(bk, "log_audit", audit)
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)
    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/admin/backups/upload-grant")
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["grant_token"]
    assert token and body["expires_in_seconds"] == 1800
    # The response contains the raw token once; disk storage contains its hash and expiry.
    persisted = json.loads(grant_file.read_text())
    assert set(persisted) == {"sha256", "expires_at"}
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in grant_file.read_text()
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "backup.upload_grant"
