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


def _touch(path, ts: str) -> None:
    """Set the file mtime to its %Y%m%d_%H%M%S timestamp (as a real backup would)."""
    epoch = datetime.strptime(ts, "%Y%m%d_%H%M%S").timestamp()
    os.utime(path, (epoch, epoch))


def _seed_group(d, ts: str, *, encrypted: bool) -> None:
    """Write a complete restore-point group (jarvis/litellm/secrets + 2 qdrant)."""
    suffix = ".enc" if encrypted else ""
    for name, payload in (
        (f"jarvis_{ts}.sql.gz{suffix}", b"J" * 10),
        (f"litellm_{ts}.sql.gz{suffix}", b"L" * 20),
        (f"secrets_{ts}.tar.gz{suffix}", b"S" * 30),
        (f"qdrant_kg_entities_{ts}.snapshot{suffix}", b"Q" * 40),
        (f"qdrant_paper_chunks_{ts}.snapshot{suffix}", b"Q" * 50),
    ):
        (d / name).write_bytes(payload)
        _touch(d / name, ts)


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
