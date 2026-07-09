"""Pure-model tests for the restore-status contract in ``routers/backups.py``.

``RestoreStatus`` is the shape the admin UI polls while a restore runs. It gains
two additive fields — ``manual_steps_required`` (a restore that finished but is
still held in maintenance) and ``phase`` (a machine-readable step key) — which
must round-trip, default safely for legacy status files, and keep ignoring the
sidecar's extra keys.
"""

from __future__ import annotations

from paper_ingestion.routers.backups import RestoreStatus


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
