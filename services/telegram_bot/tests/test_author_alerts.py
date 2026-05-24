"""Unit tests for telegram_bot.orchestration.author_alerts.

# Verified: services/telegram_bot/telegram_bot/orchestration/author_alerts.py:117
# Verified: db/migrations/0091_author_alert_log_user_dedupe.sql:11
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


def test_author_alerts_module_uses_user_scoped_dedupe() -> None:
    """author_alert_log dedupe must be scoped to (tracked_author_id, paper_id, user_id)."""
    src = Path("services/telegram_bot/telegram_bot/orchestration/author_alerts.py").read_text()
    assert _DEDUPE_COLUMNS_RE.search(src), "alert-log column list missing user_id (3-column form)"
    assert _DEDUPE_CONFLICT_RE.search(src), (
        "alert-log conflict target missing user_id (3-column form)"
    )
