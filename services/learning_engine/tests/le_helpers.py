"""LE-local test helpers — not cross-service, not in jarvis_common.testing.

These factories are specific to Learning Engine row shapes and job contexts.
They live here rather than in tests.conftest because the ``tests`` namespace
is shared with paper_ingestion (--import-mode=importlib), so any symbol in
tests.conftest must exist in both service conftest files.  LE-only helpers
belong here instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from jarvis_common.testing import FakeRecord


def make_card_row(**overrides) -> FakeRecord:
    """Return a FakeRecord compatible with row_to_card_response.

    Superset of all 5 inline _make_card_row copies across LE tests.
    Callers pass keyword overrides for any field that differs from the
    defaults (e.g. ``make_card_row(id=5, paper_id=7, user_id=42)``).
    """
    values: dict = {
        "id": 1,
        "deck_id": 1,
        "paper_id": None,
        "card_type": "concept",
        "front": "What changed?",
        "back": "The method improved retrieval.",
        "evidence": {"quote": "Improved retrieval", "page_number": 2},
        "fsrs_state": {},
        "due_at": None,
        "user_id": None,
    }
    values.update(overrides)
    # Normalise None sentinels so callers never get bare None in required fields.
    values["evidence"] = values["evidence"] if values["evidence"] is not None else {}
    values["fsrs_state"] = values["fsrs_state"] if values["fsrs_state"] is not None else {}
    values["due_at"] = values["due_at"] if values["due_at"] is not None else datetime.now(UTC)
    values.setdefault("created_at", datetime.now(UTC))
    values.setdefault("updated_at", datetime.now(UTC))
    return FakeRecord(**values)


def make_job_ctx(job_id: str = "test-job-001") -> MagicMock:
    """Return a minimal ProgressContext stub for generation router tests."""
    from jarvis_common.jobs import ProgressContext

    ctx = MagicMock(spec=ProgressContext)
    ctx.job_id = job_id
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(return_value=False)
    return ctx
