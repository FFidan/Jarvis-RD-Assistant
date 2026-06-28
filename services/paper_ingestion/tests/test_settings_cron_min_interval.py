"""Cron minimum interval validation for pulse.cron.

Verifies that the settings endpoint rejects cron expressions that fire more
than once per hour with HTTP 422 (validation error).

Also covers the rollback branch of _apply_cron_reschedule and the
scheduler-apply warning surface added to write_config (_apply_schedules).
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.services.config_validators import _validate_cron


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
    """Positive path: hourly-or-longer cron expressions must be accepted."""
    _validate_cron(expr)  # must not raise


def test_pulse_cron_rejects_invalid_expression():
    """_validate_cron must still reject malformed cron expressions."""
    with pytest.raises(ValueError, match="invalid cron expression"):
        _validate_cron("not a cron")


# ---------------------------------------------------------------------------
# _apply_cron_reschedule rollback coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cron_reschedule_rolls_back_db_when_scheduler_raises():
    """If scheduler.reschedule_job raises, rollback_sql_factory is called with old_cron."""
    from paper_ingestion.services.scheduler_effects import _apply_cron_reschedule

    mock_scheduler = MagicMock()
    mock_scheduler.reschedule_job.side_effect = RuntimeError("scheduler crash")

    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    rollback_calls: list[tuple[object, str | None]] = []

    async def rollback_factory(conn: object, old_cron: str | None) -> None:
        rollback_calls.append((conn, old_cron))

    with pytest.raises(RuntimeError, match="scheduler crash"):
        await _apply_cron_reschedule(
            scheduler=mock_scheduler,
            job_id="pulse_overnight",
            new_cron="0 9 * * *",
            old_cron="0 8 * * *",
            db_pool=mock_pool,
            rollback_sql_factory=rollback_factory,
        )

    assert len(rollback_calls) == 1, "rollback_sql_factory must be called exactly once"
    assert rollback_calls[0][1] == "0 8 * * *", "old_cron must be forwarded to rollback factory"


@pytest.mark.asyncio
async def test_apply_cron_reschedule_no_rollback_on_success():
    """If scheduler.reschedule_job succeeds, rollback_sql_factory is never called."""
    from paper_ingestion.services.scheduler_effects import _apply_cron_reschedule

    mock_scheduler = MagicMock()
    mock_pool = MagicMock()

    rollback_calls: list[object] = []

    async def rollback_factory(conn: object, old_cron: str | None) -> None:  # pragma: no cover
        rollback_calls.append(old_cron)

    await _apply_cron_reschedule(
        scheduler=mock_scheduler,
        job_id="pulse_overnight",
        new_cron="0 9 * * *",
        old_cron="0 8 * * *",
        db_pool=mock_pool,
        rollback_sql_factory=rollback_factory,
    )

    assert rollback_calls == [], "rollback must not be called when reschedule succeeds"


@pytest.mark.asyncio
async def test_apply_cron_reschedule_rolls_back_with_none_old_cron():
    """Rollback factory receives old_cron=None (delete-branch) when old_cron was unset."""
    from paper_ingestion.services.scheduler_effects import _apply_cron_reschedule

    mock_scheduler = MagicMock()
    mock_scheduler.reschedule_job.side_effect = RuntimeError("crash")

    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    rollback_calls: list[tuple[object, str | None]] = []

    async def rollback_factory(conn: object, old_cron: str | None) -> None:
        rollback_calls.append((conn, old_cron))

    with pytest.raises(RuntimeError, match="crash"):
        await _apply_cron_reschedule(
            scheduler=mock_scheduler,
            job_id="pulse_overnight",
            new_cron="0 9 * * *",
            old_cron=None,
            db_pool=mock_pool,
            rollback_sql_factory=rollback_factory,
        )

    assert len(rollback_calls) == 1
    assert rollback_calls[0][1] is None, "None old_cron must propagate to rollback factory"


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
    from paper_ingestion.services.config_write import _apply_schedules

    mock_pool = MagicMock()

    with patch(
        "paper_ingestion.services.config_write.apply_zotero_cron",
        side_effect=RuntimeError("zotero scheduler down"),
    ):
        failed = await _apply_schedules(
            db_pool=mock_pool,
            scheduler=MagicMock(),
            key="zotero.poll_cron",
            value="0 3 * * *",
            cron_ctx=(None, None),
        )

    assert failed == ["zotero_cron"]


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
