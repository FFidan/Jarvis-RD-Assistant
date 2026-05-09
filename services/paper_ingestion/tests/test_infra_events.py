"""Tests for /infra-events ingest endpoint (Vector sidecar → system_events)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def app_and_pool(monkeypatch):
    """FastAPI app mounting only the infra_events router with a mocked pool."""
    monkeypatch.setenv("INFRA_INGEST_KEY", "test-infra-secret")
    # Force reload of the module so it picks up the env var path
    import importlib

    from paper_ingestion.routers import infra_events as infra_events_mod

    importlib.reload(infra_events_mod)

    app = FastAPI()
    app.include_router(infra_events_mod.router)

    pool = MagicMock()
    conn = AsyncMock()
    conn.executemany = AsyncMock(return_value=None)
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_pool = pool

    return app, pool, conn


def test_post_rejects_missing_key(app_and_pool):
    app, _pool, _conn = app_and_pool
    client = TestClient(app)
    resp = client.post("/infra-events", json=[{"source": "x", "message": "y"}])
    assert resp.status_code == 403


def test_post_rejects_wrong_key(app_and_pool):
    app, _pool, _conn = app_and_pool
    client = TestClient(app)
    resp = client.post(
        "/infra-events",
        json=[{"source": "x", "message": "y"}],
        headers={"X-Infra-Key": "wrong"},
    )
    assert resp.status_code == 403


def test_post_accepts_json_array(app_and_pool):
    app, _pool, conn = app_and_pool
    client = TestClient(app)
    resp = client.post(
        "/infra-events",
        json=[
            {"level": "warning", "source": "nginx", "message": "5xx", "context": {"status": 502}},
            {"source": "postgres", "message": "ERROR: ..."},
        ],
        headers={"X-Infra-Key": "test-infra-secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2}
    conn.executemany.assert_awaited_once()
    rows = conn.executemany.call_args[0][1]
    assert rows[0][0] == "warning"  # level
    assert rows[0][1] == "infra"  # category forced
    assert rows[0][2] == "nginx"
    # info is the default level when not provided
    assert rows[1][0] == "info"


def test_post_accepts_ndjson(app_and_pool):
    app, _pool, conn = app_and_pool
    client = TestClient(app)
    body = "\n".join(
        [
            json.dumps({"source": "a", "message": "1"}),
            json.dumps({"source": "b", "message": "2"}),
        ]
    )
    resp = client.post(
        "/infra-events",
        content=body,
        headers={
            "Content-Type": "application/x-ndjson",
            "X-Infra-Key": "test-infra-secret",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2}


def test_post_empty_body_returns_zero(app_and_pool):
    app, _pool, _conn = app_and_pool
    client = TestClient(app)
    resp = client.post(
        "/infra-events",
        content="",
        headers={
            "Content-Type": "application/json",
            "X-Infra-Key": "test-infra-secret",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 0}


def test_unknown_level_falls_back_to_info(app_and_pool):
    app, _pool, conn = app_and_pool
    client = TestClient(app)
    resp = client.post(
        "/infra-events",
        json=[{"level": "extreme", "source": "x", "message": "y"}],
        headers={"X-Infra-Key": "test-infra-secret"},
    )
    assert resp.status_code == 200
    rows = conn.executemany.call_args[0][1]
    assert rows[0][0] == "info"
