"""H17 — Cron minimum interval validation for pulse.cron.

Verifies that the settings endpoint rejects cron expressions that fire more
than once per hour with HTTP 422 (validation error).
"""

from __future__ import annotations

import pytest
from paper_ingestion.routers.settings import _validate_cron


def test_pulse_cron_rejects_sub_hourly_schedule():
    """H17: _validate_cron must raise ValueError for cron that fires every 30 min."""
    with pytest.raises(ValueError, match="no more than once per hour"):
        _validate_cron("*/30 * * * *")  # every 30 minutes — too frequent


def test_pulse_cron_rejects_every_minute():
    """H17: _validate_cron must reject * * * * * (fires every minute)."""
    with pytest.raises(ValueError, match="no more than once per hour"):
        _validate_cron("* * * * *")


def test_pulse_cron_rejects_every_15_minutes():
    """H17: _validate_cron must reject */15 cron schedule."""
    with pytest.raises(ValueError, match="no more than once per hour"):
        _validate_cron("*/15 * * * *")


def test_pulse_cron_accepts_hourly():
    """H17 positive path: exactly-hourly cron must be accepted."""
    _validate_cron("0 * * * *")  # every hour on the hour — must not raise


def test_pulse_cron_accepts_daily():
    """H17 positive path: daily cron must be accepted."""
    _validate_cron("0 4 * * *")  # daily at 4am — must not raise


def test_pulse_cron_accepts_every_two_hours():
    """H17 positive path: every 2 hours must be accepted."""
    _validate_cron("0 */2 * * *")  # every 2 hours — must not raise


def test_pulse_cron_rejects_invalid_expression():
    """_validate_cron must still reject malformed cron expressions."""
    with pytest.raises(ValueError, match="invalid cron expression"):
        _validate_cron("not a cron")
