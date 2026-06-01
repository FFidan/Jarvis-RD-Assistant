"""Unit tests for author_alert_log dedupe scoping (SEC-AUTHALERT-1).

Both insert sites were consolidated into
``libs/jarvis_common.db_helpers.record_author_alert``; the SQL string now
lives in one place. The 3-col unique index lives in db/init.sql.
"""

from __future__ import annotations

import re
from pathlib import Path

_DEDUPE_COLUMNS_RE = re.compile(
    r"author_alert_log\s*\(\s*tracked_author_id\s*,\s*paper_id\s*,\s*user_id\s*\)"
)
_DEDUPE_CONFLICT_RE = re.compile(
    r"ON\s+CONFLICT\s*\(\s*tracked_author_id\s*,\s*paper_id\s*,\s*user_id\s*\)",
    re.IGNORECASE,
)

_DEDUPE_SOURCES = ("libs/jarvis_common/jarvis_common/db_helpers.py",)


def test_author_alerts_module_uses_user_scoped_dedupe() -> None:
    """All INSERT sites must scope dedupe to (tracked_author_id, paper_id, user_id).

    Migration 0091 drops the old 2-column unique constraint and creates a
    3-column unique index; any 2-column ON CONFLICT after that point would
    fail at runtime with `there is no unique or exclusion constraint matching
    the ON CONFLICT specification`.
    """
    for relpath in _DEDUPE_SOURCES:
        src = Path(relpath).read_text()
        assert _DEDUPE_COLUMNS_RE.search(src), (
            f"alert-log column list missing user_id (3-column form) in {relpath}"
        )
        assert _DEDUPE_CONFLICT_RE.search(src), (
            f"alert-log conflict target missing user_id (3-column form) in {relpath}"
        )
