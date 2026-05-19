"""H17 — Cron minimum interval validation for pulse.cron.

Verifies that the settings endpoint rejects cron expressions that fire more
than once per hour with HTTP 422 (validation error).
"""

from __future__ import annotations

import pytest
from paper_ingestion.routers.settings import _validate_cron


@pytest.mark.parametrize(
    "expr",
    [
        "*/30 * * * *",  # every 30 minutes
        "* * * * *",  # every minute
        "*/15 * * * *",  # every 15 minutes
    ],
)
def test_pulse_cron_rejects_sub_hourly_schedule(expr: str):
    """H17: _validate_cron rejects cron expressions that fire more than once per hour (D5-10)."""
    with pytest.raises(ValueError, match="no more than once per hour"):
        _validate_cron(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "0 * * * *",  # every hour on the hour
        "0 4 * * *",  # daily at 4am
        "0 */2 * * *",  # every 2 hours
    ],
)
def test_pulse_cron_accepts_valid_schedule(expr: str):
    """H17 positive path: hourly-or-longer cron expressions must be accepted (D5-11)."""
    _validate_cron(expr)  # must not raise


def test_pulse_cron_rejects_invalid_expression():
    """_validate_cron must still reject malformed cron expressions."""
    with pytest.raises(ValueError, match="invalid cron expression"):
        _validate_cron("not a cron")
