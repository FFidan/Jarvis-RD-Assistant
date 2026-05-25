"""Tests for /infra-events ingest endpoint (Vector sidecar → system_events)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def app_and_pool(monkeypatch):
    """FastAPI app mounting only the infra_events router with a mocked pool."""
    monkeypatch.setenv("INFRA_INGEST_KEY", "test-infra-secret")
    # Opt in to infra-ingest with a non-empty CIDR config — the default is now
    # "" (default-deny → 503), so existing tests need an explicit allowlist set
    # before the module-level reload below.
    monkeypatch.setenv("INFRA_INGEST_ALLOWED_CIDRS", "127.0.0.1/8,::1/128")
    # Force reload of the module so it picks up the env var path and resets
    # the module-level CIDR cache (_INFRA_CACHED_ALLOWED_NETWORKS = None).
    import importlib

    from paper_ingestion.routers import infra_events as infra_events_mod

    importlib.reload(infra_events_mod)
    # TestClient uses "testclient" as host — not a valid IP. Bypass the IP
    # allowlist check so existing tests exercise only auth-key logic.
    monkeypatch.setattr(infra_events_mod, "_infra_ip_in_allowlist", lambda _ip: True)

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
    assert resp.json() == {"accepted": 2, "skipped": 0}
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
    assert resp.json() == {"accepted": 2, "skipped": 0}


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
    assert resp.json() == {"accepted": 0, "skipped": 0}


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


def test_context_is_dict_not_string(app_and_pool):
    """Regression: bulk_ingest must pass e.context as a native dict, not a pre-serialised string.

    The asyncpg JSONB codec (registered via init_pg_connection) handles encoding;
    double-wrapping the value with json.dumps would store a JSON-string-of-JSON  # nolint:jsonb-double-encode
    instead of an object, causing jsonb_typeof() to return 'string' rather than 'object'.
    """
    app, _pool, conn = app_and_pool
    client = TestClient(app)
    ctx = {"status": 502, "host": "backend-1"}
    resp = client.post(
        "/infra-events",
        json=[{"level": "error", "source": "nginx", "message": "upstream error", "context": ctx}],
        headers={"X-Infra-Key": "test-infra-secret"},
    )
    assert resp.status_code == 200
    rows = conn.executemany.call_args[0][1]
    # Index 4 is the context parameter ($5::jsonb). Must be a dict, NOT a str.
    assert isinstance(rows[0][4], dict), (
        f"context must be passed as a dict for asyncpg JSONB codec; got {type(rows[0][4])}"
    )
    assert rows[0][4] == ctx


def test_context_none_becomes_empty_dict(app_and_pool):
    """When context is absent, the row must carry {} (dict), not '{}' (string)."""
    app, _pool, conn = app_and_pool
    client = TestClient(app)
    resp = client.post(
        "/infra-events",
        json=[{"source": "scheduler", "message": "tick"}],
        headers={"X-Infra-Key": "test-infra-secret"},
    )
    assert resp.status_code == 200
    rows = conn.executemany.call_args[0][1]
    assert rows[0][4] == {}
    assert isinstance(rows[0][4], dict)


def test_check_auth_uses_compare_digest(app_and_pool, monkeypatch):
    """_check_auth must delegate equality to hmac.compare_digest (CWE-208 timing safety)."""
    app, _pool, conn = app_and_pool

    import paper_ingestion.routers.infra_events as infra_events_mod

    mock_cd = MagicMock(return_value=True)
    monkeypatch.setattr(infra_events_mod.hmac, "compare_digest", mock_cd)

    client = TestClient(app)
    client.post(
        "/infra-events",
        json=[{"source": "x", "message": "y"}],
        headers={"X-Infra-Key": "test-infra-secret"},
    )

    mock_cd.assert_called_once_with(b"test-infra-secret", b"test-infra-secret")


def test_infra_ip_allowlist_rejects_non_loopback(monkeypatch):
    """_infra_ip_in_allowlist returns False for IPs outside the configured CIDRs."""
    import importlib

    monkeypatch.setenv("INFRA_INGEST_ALLOWED_CIDRS", "127.0.0.1/8,::1/128")

    from paper_ingestion.routers import infra_events as m

    importlib.reload(m)

    assert m._infra_ip_in_allowlist("127.0.0.1") is True
    assert m._infra_ip_in_allowlist("::1") is True
    assert m._infra_ip_in_allowlist("10.0.0.1") is False
    assert m._infra_ip_in_allowlist(None) is False


def test_post_rejects_non_allowlisted_ip(monkeypatch):
    """Requests from IPs not in INFRA_INGEST_ALLOWED_CIDRS receive 403."""
    import importlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("INFRA_INGEST_KEY", "test-infra-secret")
    monkeypatch.setenv("INFRA_INGEST_ALLOWED_CIDRS", "10.0.0.0/8")

    from paper_ingestion.routers import infra_events as m

    importlib.reload(m)

    app = FastAPI()
    app.include_router(m.router)

    client = TestClient(app)
    resp = client.post(
        "/infra-events",
        json=[{"source": "x", "message": "y"}],
        headers={"X-Infra-Key": "test-infra-secret"},
    )
    # TestClient IP ("testclient") is not a valid IP → rejected
    assert resp.status_code == 403
    assert "allowlist" in resp.json()["detail"]


def test_post_returns_503_when_cidr_config_empty(monkeypatch):
    """Default-deny: when INFRA_INGEST_ALLOWED_CIDRS is unset/empty, endpoint returns 503."""
    import importlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("INFRA_INGEST_KEY", "test-infra-secret")
    monkeypatch.setenv("INFRA_INGEST_ALLOWED_CIDRS", "")

    from paper_ingestion.routers import infra_events as m

    importlib.reload(m)

    app = FastAPI()
    app.include_router(m.router)

    client = TestClient(app)
    resp = client.post(
        "/infra-events",
        json=[{"source": "x", "message": "y"}],
        headers={"X-Infra-Key": "test-infra-secret"},
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


def test_parse_infra_allowed_networks_skips_invalid_cidr(monkeypatch):
    """Invalid CIDR entries are skipped + logged; valid entries survive."""
    import importlib

    monkeypatch.setenv("INFRA_INGEST_ALLOWED_CIDRS", "127.0.0.1/8,notacidr,10.0.0.0/8")

    from paper_ingestion.routers import infra_events as m

    importlib.reload(m)

    nets = m._parse_infra_allowed_networks()
    assert len(nets) == 2  # "notacidr" silently dropped, two valid networks kept


def test_infra_ip_in_allowlist_rejects_unparseable_string(monkeypatch):
    """Unparseable IP string (e.g. 'testclient' from FastAPI TestClient) returns False."""
    import importlib

    monkeypatch.setenv("INFRA_INGEST_ALLOWED_CIDRS", "127.0.0.1/8")

    from paper_ingestion.routers import infra_events as m

    importlib.reload(m)

    assert m._infra_ip_in_allowlist("testclient") is False
    assert m._infra_ip_in_allowlist("not-an-ip") is False


def test_load_ingest_key_logs_on_oserror(monkeypatch, caplog):
    import importlib

    from paper_ingestion.routers import infra_events as m

    importlib.reload(m)

    monkeypatch.setenv("INFRA_INGEST_KEY", "")
    monkeypatch.setenv("INFRA_INGEST_KEY_FILE", "/run/secrets/infra_ingest_key")

    # Reload config to pick up the new env vars
    from paper_ingestion import config as cfg_mod

    importlib.reload(cfg_mod)

    # Make the file appear to exist but fail on read
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(
        Path, "read_text", lambda self, **kw: (_ for _ in ()).throw(OSError("permission denied"))
    )

    with caplog.at_level(logging.ERROR, logger="paper_ingestion.routers.infra_events"):
        result = m._load_ingest_key()

    assert result is None
    assert any(
        "/run/secrets/infra_ingest_key" in r.message
        for r in caplog.records
        if r.levelno == logging.ERROR
    )


def test_ingest_infra_events_counts_skipped_malformed_lines(app_and_pool, caplog):
    app, _pool, _conn = app_and_pool
    client = TestClient(app)

    body = "\n".join(
        [
            json.dumps({"source": "a", "message": "1"}),
            "not json {{{",
            json.dumps({"source": "b", "message": "2"}),
            "also bad",
        ]
    )

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.routers.infra_events"):
        resp = client.post(
            "/infra-events",
            content=body,
            headers={
                "Content-Type": "application/x-ndjson",
                "X-Infra-Key": "test-infra-secret",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2, "skipped": 2}
    warning_records = [
        r for r in caplog.records if r.levelno == logging.WARNING and "skipped" in r.message
    ]
    assert len(warning_records) == 1
    assert "2" in warning_records[0].message
