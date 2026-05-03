"""Tests for Sprint 5 Wave 1 Batch 1A fixes.

Covers:
- H5: validate_encrypted_config_rows tolerates missing table on fresh DB
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import FastAPI  # noqa: F401

# ---------------------------------------------------------------------------
# H5 — validate_encrypted_config_rows tolerates missing table
# ---------------------------------------------------------------------------


class TestValidateEncryptedConfigRowsToleratesMissingTable:
    async def test_lifespan_tolerates_undefined_table_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UndefinedTableError from validate_encrypted_config_rows must not abort startup."""
        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        # Simulate fresh DB: user_config table does not exist yet.
        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool",
                AsyncMock(return_value=fake_pool),
            ),
            patch(
                "jarvis_common.app_factory.validate_encrypted_config_rows",
                AsyncMock(side_effect=asyncpg.UndefinedTableError("relation does not exist")),
            ),
            patch(
                "jarvis_common.app_factory.validate_production_config",
                MagicMock(return_value=None),
            ),
            patch(
                "jarvis_common.app_factory.httpx.AsyncClient",
                MagicMock(return_value=fake_http_client),
            ),
        ):
            config = ServiceLifespanConfig(
                service_name="test_fresh_db",
            )
            app = FastAPI()
            # Must not raise — the UndefinedTableError should be caught and warned.
            async with configure_lifespan(config)(app):
                pass  # startup succeeded

    async def test_lifespan_tolerates_undefined_column_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UndefinedColumnError from validate_encrypted_config_rows must not abort startup."""
        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool",
                AsyncMock(return_value=fake_pool),
            ),
            patch(
                "jarvis_common.app_factory.validate_encrypted_config_rows",
                AsyncMock(side_effect=asyncpg.UndefinedColumnError("column does not exist")),
            ),
            patch(
                "jarvis_common.app_factory.validate_production_config",
                MagicMock(return_value=None),
            ),
            patch(
                "jarvis_common.app_factory.httpx.AsyncClient",
                MagicMock(return_value=fake_http_client),
            ),
        ):
            config = ServiceLifespanConfig(
                service_name="test_fresh_db_col",
            )
            app = FastAPI()
            async with configure_lifespan(config)(app):
                pass  # startup succeeded

    async def test_validate_encrypted_config_rows_called_after_custom_init_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate_encrypted_config_rows must be called BEFORE custom_init_tasks run.

        NEW-M6: validation order was intentionally reversed so that a bad schema
        fails fast before any custom hook runs.
        """
        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_hook(app: FastAPI) -> None:
            call_log.append("init_hook")

        async def fake_validate(pool: object, **kwargs: object) -> int:
            call_log.append("validate_encrypted")
            return 0

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool",
                AsyncMock(return_value=fake_pool),
            ),
            patch(
                "jarvis_common.app_factory.validate_encrypted_config_rows",
                fake_validate,
            ),
            patch(
                "jarvis_common.app_factory.validate_production_config",
                MagicMock(return_value=None),
            ),
            patch(
                "jarvis_common.app_factory.httpx.AsyncClient",
                MagicMock(return_value=fake_http_client),
            ),
        ):
            config = ServiceLifespanConfig(
                service_name="test_ordering",
                custom_init_tasks=[init_hook],
                custom_teardown_tasks=[None],
            )
            app = FastAPI()
            async with configure_lifespan(config)(app):
                pass

        # validate_encrypted must come before init_hook (NEW-M6 fix).
        assert call_log == ["validate_encrypted", "init_hook"], (
            f"Expected validate_encrypted before init_hook, got: {call_log}"
        )
