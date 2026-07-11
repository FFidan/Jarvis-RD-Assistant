"""Tests for the restore-status contract + one-time bearer-token auth in
``routers/backups.py``.

``RestoreStatus`` is the shape the admin UI polls while a restore runs. It gains
two additive fields — ``manual_steps_required`` (a restore that finished but is
still held in maintenance) and ``phase`` (a machine-readable step key) — which
must round-trip, default safely for legacy status files, and keep ignoring the
sidecar's extra keys.

The ASGI tests below exercise the restore-status bearer token: ``request_restore``
mints it and persists only its hash; ``restore_status`` accepts it DB-free (proven
with the DB pool wired to explode on any access) so the initiating admin's poll
survives after a restore has torn down the session store, while a wrong/expired
token falls through to the normal session/API-key gate.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.auth import restore_status_bearer_valid, restore_status_token_file
from jarvis_common.testing_contract_apps import configure_contract_api_key

from paper_ingestion.routers import backups as bk
from paper_ingestion.routers.backups import RestoreStatus

_STATUS_PATH = "/api/admin/backups/restore/status"


class _DeadPool:
    """A DB pool that raises on ANY access — proves a code path is DB-free."""

    def __getattr__(self, name: str):  # noqa: ANN204
        raise AssertionError(f"restore_status touched the DB pool ({name}) — must be DB-free")


def _seed_complete_point(d, ts: str) -> None:
    """Write a complete restore point (jarvis + litellm + secrets) under ``d``."""
    for name in (f"jarvis_{ts}.sql.gz", f"litellm_{ts}.sql.gz", f"secrets_{ts}.tar.gz"):
        (d / name).write_bytes(b"x" * 8)


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


def test_write_status_token_persists_only_hash(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(tmp_path))
    token = "correct-horse-battery-staple"

    assert bk._write_status_token(token) is True

    f = restore_status_token_file()
    persisted = json.loads(f.read_text())
    # Only the hash + expiry are persisted — never the raw token.
    assert set(persisted) == {"sha256", "expires_at"}
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
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
    token = resp.json()["status_token"]
    assert token
    f = restore_status_token_file()
    persisted = json.loads(f.read_text())
    assert set(persisted) == {"sha256", "expires_at"}
    assert persisted["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in f.read_text()
    # The token file is SEPARATE from the request sentinel restore.sh consumes.
    assert (trigger / ".restore_request.json").exists()
    assert f.name == ".restore_status_token.json"


@pytest.mark.asyncio
async def test_restore_status_bearer_token_is_db_free(tmp_path, monkeypatch) -> None:
    """A valid bearer token authorizes the poll through the REAL front door + route
    gate with the DB pool wired to explode on any access — proving DB-free auth."""
    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "_RESTORE_STATUS", trigger / ".restore_status.json")

    token = "db-free-poll-token-value"
    assert bk._write_status_token(token) is True
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
    # configure_contract_api_key ARMS the global front door (a real key configured),
    # so passing the bearer is a meaningful proof — an unauthenticated request 403s.
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
async def test_restore_status_bad_bearer_falls_through_to_front_door(
    tmp_path, monkeypatch, scenario
) -> None:
    """A wrong / expired / absent bearer token is NOT accepted — the request falls
    through to the normal gate and is rejected (no session, no API-key)."""
    from paper_ingestion.main import app

    trigger = tmp_path / "trigger"
    trigger.mkdir()
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setattr(bk, "_RESTORE_SENTINEL", trigger / ".restore_request.json")
    monkeypatch.setattr(bk, "_RESTORE_STATUS", trigger / ".restore_status.json")

    if scenario == "wrong":
        assert bk._write_status_token("the-real-token") is True
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

    # A rejection needs no DB: None pool -> the front door's failure-audit is skipped.
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
    """The front-door helper must FALL THROUGH (return False), never raise, on any
    malformed sentinel file — otherwise a corrupt/garbage token file would 500 the
    status poll instead of degrading to the session/API-key gate."""
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(tmp_path))
    (tmp_path / ".restore_status_token.json").write_text(content)
    request = SimpleNamespace(headers={"Authorization": "Bearer anything"})

    assert restore_status_bearer_valid(request) is False
