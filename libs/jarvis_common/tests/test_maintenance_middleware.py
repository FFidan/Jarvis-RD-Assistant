"""Boundary tests for MaintenanceMiddleware (P6 sentinel-driven 503 gate).

Pure-ASGI middleware: a fresh sentinel returns 503 for non-exempt paths,
exempt prefixes are always served, and a sentinel older than ``max_age_s`` is
ignored (auto-expiry anti-brick). No sentinel means normal serving.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common.maintenance import MaintenanceMiddleware, maintenance_active

_MAX_AGE_S = 1800


def _make_client(sentinel: Path, destructive: Path | None = None) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        MaintenanceMiddleware,
        sentinel_path=str(sentinel),
        destructive_sentinel_path=str(destructive or sentinel.parent / ".destructive"),
        max_age_s=_MAX_AGE_S,
    )

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/admin/backups/restore/status")
    async def restore_status():
        return {"state": "running"}

    @app.get("/api/setup/status")
    async def setup_status():
        return {"setup_completed": False}

    return TestClient(app, raise_server_exceptions=True)


def test_absent_sentinel_serves_normally(tmp_path):
    client = _make_client(tmp_path / "missing.maintenance")
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_fresh_sentinel_blocks_non_exempt_with_retry_after(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/ping")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "30"
    body = json.loads(resp.content)
    assert body["detail"] == "Restore in progress"
    assert body["retry_after"] == 30


def test_health_exempt_under_fresh_sentinel(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_restore_status_exempt_under_fresh_sentinel(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/api/admin/backups/restore/status")
    assert resp.status_code == 200
    assert resp.json() == {"state": "running"}


def test_setup_status_exempt_under_fresh_sentinel(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel)

    resp = client.get("/api/setup/status")
    assert resp.status_code == 200
    assert resp.json() == {"setup_completed": False}


def test_stale_sentinel_is_ignored(tmp_path):
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    stale = time.time() - _MAX_AGE_S - 60
    os.utime(sentinel, (stale, stale))
    client = _make_client(sentinel)

    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_stale_destructive_sentinel_still_blocks(tmp_path):
    # No-age-gate proof: a stale SOFT sentinel would serve, but the destructive
    # sentinel blocks regardless of mtime (durable fail-closed).
    sentinel = tmp_path / ".maintenance"
    destructive = tmp_path / ".destructive"
    destructive.touch()
    stale = time.time() - _MAX_AGE_S - 3600
    os.utime(destructive, (stale, stale))
    client = _make_client(sentinel, destructive=destructive)

    resp = client.get("/ping")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "30"


def test_destructive_sentinel_exempt_paths_still_served(tmp_path):
    sentinel = tmp_path / ".maintenance"
    destructive = tmp_path / ".destructive"
    destructive.touch()
    client = _make_client(sentinel, destructive=destructive)

    assert client.get("/health").status_code == 200
    assert client.get("/api/admin/backups/restore/status").status_code == 200


def test_absent_destructive_falls_through_to_soft_logic(tmp_path):
    # Destructive absent + fresh soft sentinel -> soft logic still 503s
    # (the destructive check must not short-circuit normal serving/soft logic).
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    client = _make_client(sentinel, destructive=tmp_path / ".destructive")

    resp = client.get("/ping")
    assert resp.status_code == 503


def test_maintenance_active_mirrors_middleware_sentinel_logic(tmp_path, monkeypatch):
    # The module-level helper follows the same lifecycle as the middleware:
    # absent -> False; fresh soft -> True; stale soft -> False; destructive -> True
    # regardless of age.
    soft = tmp_path / ".maintenance"
    destructive = tmp_path / ".destructive"
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(soft))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(destructive))

    assert maintenance_active() is False

    soft.touch()
    assert maintenance_active() is True

    stale = time.time() - _MAX_AGE_S - 60
    os.utime(soft, (stale, stale))
    assert maintenance_active() is False

    destructive.touch()
    os.utime(destructive, (stale, stale))
    assert maintenance_active() is True
