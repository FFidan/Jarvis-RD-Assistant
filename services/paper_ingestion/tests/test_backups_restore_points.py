"""Pure-function tests for restore-point grouping, .last_run.json surfacing, and
the qdrant ``.snapshot.enc`` allowlist additions in ``routers/backups.py``.

These exercise the module-level helpers directly (no DB / ASGI harness): the
helpers are the load-bearing logic the admin endpoints delegate to.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from paper_ingestion.routers import backups as bk


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    """Point the router's _BACKUP_DIR at a tmp dir and return it."""
    monkeypatch.setattr(bk, "_BACKUP_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def code_max_50(tmp_path, monkeypatch):
    """Point _code_max_migration at a tmp migrations dir whose highest version is 50."""
    mig = tmp_path / "_migrations"
    mig.mkdir()
    (mig / "0001_init.sql").write_text("-- a")
    (mig / "0050_latest.sql").write_text("-- b")
    monkeypatch.setenv("DB_MIGRATIONS_DIR", str(mig))
    return 50


def _touch(path, ts: str) -> None:
    """Set the file mtime to its %Y%m%d_%H%M%S timestamp (as a real backup would)."""
    epoch = datetime.strptime(ts, "%Y%m%d_%H%M%S").timestamp()
    os.utime(path, (epoch, epoch))


def _seed_group(
    d,
    ts: str,
    *,
    encrypted: bool,
    manifest_schema_version: int | None = None,
    manifest_app_version: str = "0.9.2",
    manifest_archives: list | None = None,
) -> None:
    """Write a complete restore-point group (jarvis/litellm/secrets + 2 qdrant).

    When ``manifest_schema_version`` is given, also writes a ``manifest_<ts>.json``
    describing the group (or ``manifest_archives`` verbatim, for phantom cases).
    """
    suffix = ".enc" if encrypted else ""
    names = [
        f"jarvis_{ts}.sql.gz{suffix}",
        f"litellm_{ts}.sql.gz{suffix}",
        f"secrets_{ts}.tar.gz{suffix}",
        f"qdrant_kg_entities_{ts}.snapshot{suffix}",
        f"qdrant_paper_chunks_{ts}.snapshot{suffix}",
    ]
    payloads = [b"J" * 10, b"L" * 20, b"S" * 30, b"Q" * 40, b"Q" * 50]
    for name, payload in zip(names, payloads):
        (d / name).write_bytes(payload)
        _touch(d / name, ts)
    if manifest_schema_version is not None:
        archives = manifest_archives
        if archives is None:
            archives = [
                {"filename": n, "sha256": "0" * 64, "size_bytes": len(p)}
                for n, p in zip(names, payloads)
            ]
        (d / f"manifest_{ts}.json").write_text(
            json.dumps(
                {
                    "timestamp": ts,
                    "app_version": manifest_app_version,
                    "schema_version": manifest_schema_version,
                    "created_at": "2026-06-26T12:00:00+00:00",
                    "archives": archives,
                }
            )
        )


def test_restore_points_groups_by_timestamp(backup_dir):
    _seed_group(backup_dir, "20260624_120000", encrypted=True)
    # Incomplete, unencrypted group: jarvis only, plus one qdrant.
    (backup_dir / "jarvis_20260623_090000.sql.gz").write_bytes(b"j" * 5)
    _touch(backup_dir / "jarvis_20260623_090000.sql.gz", "20260623_090000")
    (backup_dir / "qdrant_kg_entities_20260623_090000.snapshot").write_bytes(b"q" * 7)
    _touch(backup_dir / "qdrant_kg_entities_20260623_090000.snapshot", "20260623_090000")

    points = bk._group_restore_points(bk._list_entries())

    assert [p.timestamp for p in points] == ["20260624_120000", "20260623_090000"]

    complete = points[0]
    assert complete.complete is True
    assert complete.encrypted is True
    assert complete.stores == ["jarvis", "litellm", "qdrant", "secrets"]
    assert complete.qdrant_collections == ["kg_entities", "paper_chunks"]
    assert complete.total_size_bytes == 10 + 20 + 30 + 40 + 50
    assert len(complete.files) == 5

    incomplete = points[1]
    assert incomplete.complete is False
    assert incomplete.encrypted is False
    assert incomplete.qdrant_collections == ["kg_entities"]
    assert "litellm" not in incomplete.stores


def test_last_run_surfaces_failure_in_status_and_restore_points(backup_dir):
    # A failed run leaves no fresh archive but DOES write .last_run.json.
    (backup_dir / "jarvis_20260620_000000.sql.gz").write_bytes(b"old")
    (backup_dir / ".last_run.json").write_text(
        json.dumps(
            {
                "attempted_at": "2026-06-24T03:00:00+00:00",
                "timestamp": "20260624_030000",
                "succeeded": False,
                "encrypted": True,
                "retention_days": 7,
                "stores": {
                    "jarvis": "failed",
                    "litellm": "failed",
                    "secrets": "skipped",
                    "qdrant": "skipped",
                },
            }
        )
    )

    run = bk._read_last_run()
    assert run is not None
    # /status surfaces last_attempt_at + last_run_succeeded from .last_run.json.
    assert run["succeeded"] is False
    assert run["attempted_at"] == "2026-06-24T03:00:00+00:00"
    assert run["stores"]["jarvis"] == "failed"
    assert run["retention_days"] == 7

    last_run = bk.RestoreLastRun(
        attempted_at=run.get("attempted_at"),
        succeeded=run.get("succeeded"),
        stores=run.get("stores") or {},
    )
    assert last_run.succeeded is False
    assert last_run.stores["jarvis"] == "failed"


def test_read_last_run_returns_none_when_absent_or_malformed(backup_dir):
    assert bk._read_last_run() is None  # absent
    (backup_dir / ".last_run.json").write_text("{not json")
    assert bk._read_last_run() is None  # malformed → never raises


def test_last_run_json_is_not_treated_as_an_archive(backup_dir):
    (backup_dir / ".last_run.json").write_text("{}")
    (backup_dir / "jarvis_20260624_120000.sql.gz").write_bytes(b"j")
    names = {e.filename for e in bk._list_entries()}
    assert ".last_run.json" not in names
    assert "jarvis_20260624_120000.sql.gz" in names


def test_filename_allowlist_accepts_encrypted_qdrant_snapshot():
    assert bk._FILENAME_RE.match("qdrant_kg_entities_20260624_001234.snapshot.enc")
    assert bk._FILENAME_RE.match("qdrant_paper_chunks_20260624_001234.snapshot")
    # _validate_name must not raise for the new encrypted shape.
    bk._validate_name("qdrant_kg_entities_20260624_001234.snapshot.enc")


def test_filename_allowlist_still_rejects_traversal():
    from fastapi import HTTPException

    for bad in (
        "../x",
        "/etc/passwd",
        "qdrant_x_20260624_001234.snapshot.enc/../../etc/passwd",
        "..",
    ):
        with pytest.raises(HTTPException) as ei:
            bk._validate_name(bad)
        assert ei.value.status_code == 400, bad


def test_qdrant_collection_parse_ignores_underscores_in_name():
    """Collection names contain underscores; the ts must still parse unambiguously."""
    m = bk._QDRANT_RE.match("qdrant_kg_entities_20260624_001234.snapshot.enc")
    assert m is not None
    assert m.group(1) == "kg_entities"
    ts = bk._TS_RE.search("qdrant_kg_entities_20260624_001234.snapshot.enc")
    assert ts is not None
    assert ts.group(1) == "20260624_001234"


def test_manifest_is_not_listed_as_an_archive(backup_dir):
    """A manifest_<ts>.json must never be enumerated/downloadable as a backup."""
    _seed_group(backup_dir, "20260624_120000", encrypted=True, manifest_schema_version=50)
    names = {e.filename for e in bk._list_entries()}
    assert not any(n.startswith("manifest_") for n in names)


def test_restore_point_compat_same_and_versions_populate(backup_dir, code_max_50):
    _seed_group(
        backup_dir,
        "20260624_120000",
        encrypted=False,
        manifest_schema_version=50,
        manifest_app_version="1.0.0",
    )
    point = bk._group_restore_points(bk._list_entries())[0]
    assert point.app_version == "1.0.0"
    assert point.schema_version == 50
    assert point.compat == "same"


def test_restore_point_compat_newer_when_schema_ahead(backup_dir, code_max_50):
    _seed_group(backup_dir, "20260624_120000", encrypted=True, manifest_schema_version=999)
    point = bk._group_restore_points(bk._list_entries())[0]
    assert point.schema_version == 999
    assert point.compat == "newer"


def test_restore_point_compat_older_when_schema_behind(backup_dir, code_max_50):
    _seed_group(backup_dir, "20260624_120000", encrypted=True, manifest_schema_version=10)
    point = bk._group_restore_points(bk._list_entries())[0]
    assert point.compat == "older"


def test_restore_point_absent_manifest_is_unknown(backup_dir, code_max_50):
    _seed_group(backup_dir, "20260624_120000", encrypted=True)  # no manifest seeded
    point = bk._group_restore_points(bk._list_entries())[0]
    assert point.app_version is None
    assert point.schema_version is None
    assert point.compat == "unknown"


def test_restore_point_malformed_manifest_degrades_to_unknown(backup_dir, code_max_50):
    _seed_group(backup_dir, "20260624_120000", encrypted=True)
    (backup_dir / "manifest_20260624_120000.json").write_text("{not json")
    # Must not raise — a corrupt manifest degrades to unknown, never 500s.
    point = bk._group_restore_points(bk._list_entries())[0]
    assert point.compat == "unknown"
    assert point.schema_version is None
    assert point.app_version is None


def test_restore_point_phantom_manifest_rejected(backup_dir, code_max_50):
    """A manifest referencing a file not on disk is rejected (no phantom versions)."""
    _seed_group(
        backup_dir,
        "20260624_120000",
        encrypted=True,
        manifest_schema_version=50,
        manifest_archives=[
            {"filename": "jarvis_20260624_120000.sql.gz.enc", "sha256": "0" * 64, "size_bytes": 10},
            {"filename": "ghost_20260624_120000.sql.gz.enc", "sha256": "0" * 64, "size_bytes": 1},
        ],
    )
    point = bk._group_restore_points(bk._list_entries())[0]
    assert point.compat == "unknown"
    assert point.schema_version is None


def test_code_max_migration_returns_floor_when_dir_missing(monkeypatch, tmp_path):
    # An absent/empty migrations dir falls back to the code's schema floor (the
    # db/SCHEMA_VERSION baseline) so restore-point compatibility stays armed
    # instead of degrading to "unknown".
    monkeypatch.setenv("DB_MIGRATIONS_DIR", str(tmp_path / "does_not_exist"))
    assert bk._code_max_migration() == 101


@pytest.mark.asyncio
async def test_download_backup_serves_valid_archive(backup_dir, monkeypatch):
    """A valid archive name still serves the file (200) through the secure_path guard.

    Confirms the traversal-safe join does not reject a legitimate allowlisted
    name — the happy path is unchanged after centralising the guard.
    """
    from unittest.mock import AsyncMock

    import httpx
    from httpx import ASGITransport
    from jarvis_common.auth import require_admin, verify_api_key

    from paper_ingestion.main import app

    name = "jarvis_20260624_120000.sql.gz"
    (backup_dir / name).write_bytes(b"J" * 10)

    monkeypatch.setattr(bk, "log_audit", AsyncMock())
    monkeypatch.setattr(app.state, "db_pool", AsyncMock(), raising=False)
    app.state.limiter.enabled = False
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[verify_api_key] = lambda: None

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/admin/backups/{name}/download")
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 200
    assert resp.content == b"J" * 10
