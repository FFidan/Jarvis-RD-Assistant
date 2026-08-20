"""Cron minimum interval validation for pulse.cron.

Verifies sub-hourly rejection plus the scheduler warning surface in
``write_config``.
"""

from __future__ import annotations
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common import config_validators
from jarvis_common.config_validators import _validate_cron, _validate_zotero_cron


@pytest.mark.parametrize(
    "expr",
    [
        "*/30 * * * *",  # every 30 minutes
        "* * * * *",  # every minute
        "*/15 * * * *",  # every 15 minutes
    ],
)
def test_pulse_cron_rejects_sub_hourly_schedule(expr: str):
    """_validate_cron rejects cron expressions that fire more than once per hour."""
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
    """Positive path: hourly-or-longer cron expressions must be accepted."""
    _validate_cron(expr)  # must not raise


def test_pulse_cron_rejects_invalid_expression():
    """_validate_cron must still reject malformed cron expressions."""
    with pytest.raises(ValueError, match="invalid cron expression"):
        _validate_cron("not a cron")


def test_zotero_poll_cron_rejects_a_sub_quarter_hour_schedule() -> None:
    with pytest.raises(ValueError, match="15 minutes"):
        _validate_zotero_cron("* * * * *")


def test_zotero_poll_cron_accepts_the_hourly_default() -> None:
    _validate_zotero_cron("0 * * * *")


def test_zotero_poll_cron_accepts_a_half_hourly_schedule() -> None:
    # Pins the floor at fifteen minutes rather than Pulse's hour: this case is
    # what fails if someone later copies the Pulse limit across.
    _validate_zotero_cron("0,30 * * * *")


@pytest.mark.parametrize(
    "base",
    [
        pytest.param(datetime(2026, 8, 12, 12, 10), id="first-gap-is-small"),
        pytest.param(datetime(2026, 8, 12, 12, 55), id="first-gap-is-large"),
    ],
)
def test_zotero_poll_cron_rejects_a_tight_pair_whatever_the_time_of_day(monkeypatch, base) -> None:
    """The floor must hold at every base time, not just the one the save happened at.

    ``0,50,51 * * * *`` fires an hour apart, then one minute apart. Looking only
    at the next two fire times sees the tight pair from 12:10 and misses it from
    12:55, so the same expression would be accepted or rejected by the clock.
    """
    monkeypatch.setattr(config_validators, "_now", lambda: base)

    with pytest.raises(ValueError, match="15 minutes"):
        _validate_zotero_cron("0,50,51 * * * *")


def test_zotero_poll_cron_accepts_a_weekly_schedule(monkeypatch) -> None:
    """A schedule firing less often than the scan window is above the floor by definition."""
    monkeypatch.setattr(config_validators, "_now", lambda: datetime(2026, 8, 12, 12, 10))

    _validate_zotero_cron("0 9 * * 1")


# ---------------------------------------------------------------------------
# _apply_schedules: failure surface (M16)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_schedules_pulse_failure_returns_warning_not_raise():
    """_apply_schedules catches a pulse_cron failure and returns its name, never raises."""
    from paper_ingestion.services.config_write import _apply_schedules

    mock_scheduler = MagicMock()
    mock_pool = MagicMock()

    with patch(
        "paper_ingestion.services.config_write.apply_pulse_cron",
        side_effect=RuntimeError("scheduler down"),
    ):
        failed = await _apply_schedules(
            db_pool=mock_pool,
            scheduler=mock_scheduler,
            key="pulse.cron",
            value="0 4 * * *",
            cron_ctx=(None, None),
        )

    assert failed == ["pulse_cron"], "pulse_cron must appear in the failed list on raise"


@pytest.mark.asyncio
async def test_apply_schedules_zotero_failure_returns_warning_not_raise():
    """_apply_schedules catches a zotero_cron failure and returns its name, never raises."""
    from paper_ingestion.services.config_write import _apply_schedules, _schedule_runtime

    mock_pool = MagicMock()

    with patch(
        "paper_ingestion.scheduler.reconcile_zotero_poll_job",
        side_effect=RuntimeError("zotero scheduler down"),
    ):
        failed = await _apply_schedules(
            db_pool=mock_pool,
            scheduler=_schedule_runtime(MagicMock(), object()),
            key="zotero.poll_cron",
            value="0 3 * * *",
            cron_ctx=(None, 7),
        )

    assert failed == ["zotero_poll"]


@pytest.mark.asyncio
async def test_apply_schedules_zotero_uses_imported_scheduler_module(monkeypatch):
    """Zotero reconcile uses sys.modules even if the package attr is stale."""
    import paper_ingestion
    from paper_ingestion.services.config_write import _apply_schedules, _schedule_runtime

    stale_reconcile = AsyncMock()
    monkeypatch.setattr(
        paper_ingestion,
        "scheduler",
        SimpleNamespace(reconcile_zotero_poll_job=stale_reconcile),
        raising=False,
    )
    patched_reconcile = AsyncMock(side_effect=RuntimeError("patched scheduler down"))

    with patch(
        "paper_ingestion.scheduler.reconcile_zotero_poll_job",
        new=patched_reconcile,
    ):
        failed = await _apply_schedules(
            db_pool=MagicMock(),
            scheduler=_schedule_runtime(MagicMock(), object()),
            key="zotero.poll_cron",
            value="0 3 * * *",
            cron_ctx=(None, 7),
        )

    assert failed == ["zotero_poll"]
    patched_reconcile.assert_awaited_once()
    stale_reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_schedules_fetch_interval_failure_returns_warning_not_raise():
    """_apply_schedules catches a fetch_interval failure and returns its name, never raises."""
    from paper_ingestion.services.config_write import _apply_schedules

    with patch(
        "paper_ingestion.services.config_write.apply_fetch_interval",
        side_effect=RuntimeError("scheduler down"),
    ):
        failed = await _apply_schedules(
            db_pool=MagicMock(),
            scheduler=MagicMock(),
            key="automation.fetch_interval_hours",
            value=6,
            cron_ctx=(None, None),
        )

    assert failed == ["fetch_interval"]


@pytest.mark.asyncio
async def test_write_config_scheduler_failure_returns_warning_not_500():
    """A failing pulse_cron scheduler apply yields ConfigWriteResult with schedule_apply_warnings.

    The DB commit stands (write_config must not raise); the warning names the
    failed scheduler so callers can surface it to the user.
    """
    from paper_ingestion.services.config_write import ConfigWriteResult, write_config

    # Minimal DB mock: fetchrow for old-cron pre-read, execute for UPSERT.
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None  # no existing pulse.cron row
    transaction_ctx = MagicMock()
    transaction_ctx.__aenter__ = AsyncMock(return_value=None)
    transaction_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_conn.transaction = MagicMock(return_value=transaction_ctx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = ctx

    with patch(
        "paper_ingestion.services.config_write.apply_pulse_cron",
        side_effect=RuntimeError("scheduler is down"),
    ):
        result = await write_config(
            db_pool=mock_pool,
            scheduler=MagicMock(),
            http_client=AsyncMock(),
            ollama_url="http://localhost:11434",
            key="pulse.cron",
            value="0 4 * * *",
            caller_user_id=None,
        )

    assert isinstance(result, ConfigWriteResult), "write_config must return ConfigWriteResult"
    assert result.display_value == "0 4 * * *"
    assert "pulse_cron" in result.schedule_apply_warnings, (
        "Failed scheduler apply must be named in schedule_apply_warnings"
    )
    # DB conn.execute was called (UPSERT committed before scheduler was attempted).
    assert mock_conn.execute.called, "DB UPSERT must be committed even when scheduler fails"
