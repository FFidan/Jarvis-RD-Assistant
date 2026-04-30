"""Tests for NEW-M6: validate_encrypted_config_rows ordering + compensating teardown.

Covers:
- validate_encrypted_config_rows runs BEFORE custom_init_tasks hooks
- if the second init hook raises, the first hook's teardown counterpart is called
- compensating teardown runs in reverse order of completed hooks
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lifespan_stack(fake_pool: AsyncMock, fake_http_client: AsyncMock) -> contextlib.ExitStack:
    """Return an ExitStack that stubs out I/O in configure_lifespan."""
    stack = contextlib.ExitStack()
    stack.enter_context(
        patch(
            "jarvis_common.app_factory.asyncpg.create_pool",
            AsyncMock(return_value=fake_pool),
        )
    )
    stack.enter_context(
        patch(
            "jarvis_common.app_factory.validate_encrypted_config_rows",
            AsyncMock(return_value=None),
        )
    )
    stack.enter_context(
        patch(
            "jarvis_common.app_factory.validate_production_config",
            MagicMock(return_value=None),
        )
    )
    stack.enter_context(
        patch(
            "jarvis_common.app_factory.httpx.AsyncClient",
            MagicMock(return_value=fake_http_client),
        )
    )
    return stack


# ---------------------------------------------------------------------------
# NEW-M6: compensating teardown when second init hook raises
# ---------------------------------------------------------------------------


class TestCompensatingTeardown:
    async def test_first_hook_teardown_called_when_second_hook_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the second init hook raises, the first hook's teardown is called.

        The second hook has no matching teardown (only one teardown registered),
        so only the first hook's teardown runs, in reverse order.
        """
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_first(app: FastAPI) -> None:
            call_log.append("init_first")

        async def init_second(app: FastAPI) -> None:
            call_log.append("init_second")
            raise RuntimeError("second hook boom")

        async def teardown_first(app: FastAPI) -> None:
            call_log.append("teardown_first")

        # Two teardowns registered — init_first → teardown_first, init_second → None
        # (None means init_second has no teardown counterpart; required by the
        # equal-length contract).
        config = ServiceLifespanConfig(
            service_name="test_compensate",
            jobs_worker_kinds=set(),
            custom_init_tasks=[init_first, init_second],
            custom_teardown_tasks=[teardown_first, None],
        )

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with _lifespan_stack(fake_pool, fake_http_client):
            app = FastAPI()
            lifespan = configure_lifespan(config)
            with pytest.raises(RuntimeError, match="second hook boom"):
                async with lifespan(app):
                    pass  # pragma: no cover -- startup raised before yield

        # init_first completed, init_second raised.
        # Compensating teardown must have run for init_first.
        assert "init_first" in call_log
        assert "init_second" in call_log
        assert "teardown_first" in call_log

        # Resources still cleaned up despite startup failure.
        fake_pool.close.assert_awaited_once()
        fake_http_client.aclose.assert_awaited_once()

    async def test_compensating_teardown_runs_in_reverse_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Compensating teardowns run in reverse order of completed init hooks."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_a(app: FastAPI) -> None:
            call_log.append("init_a")

        async def init_b(app: FastAPI) -> None:
            call_log.append("init_b")

        async def init_c(app: FastAPI) -> None:
            call_log.append("init_c")
            raise RuntimeError("third hook boom")

        async def teardown_a(app: FastAPI) -> None:
            call_log.append("teardown_a")

        async def teardown_b(app: FastAPI) -> None:
            call_log.append("teardown_b")

        async def teardown_c(app: FastAPI) -> None:
            call_log.append("teardown_c")  # pragma: no cover -- init_c never succeeded

        config = ServiceLifespanConfig(
            service_name="test_reverse_compensate",
            jobs_worker_kinds=set(),
            custom_init_tasks=[init_a, init_b, init_c],
            custom_teardown_tasks=[teardown_a, teardown_b, teardown_c],
        )

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with _lifespan_stack(fake_pool, fake_http_client):
            app = FastAPI()
            lifespan = configure_lifespan(config)
            with pytest.raises(RuntimeError, match="third hook boom"):
                async with lifespan(app):
                    pass  # pragma: no cover

        # init_a and init_b completed; init_c raised.
        # Compensating teardowns run in REVERSE order of completion: b before a.
        # (The finally block also runs all teardowns unconditionally after the
        #  compensating path, so teardown_b and teardown_a appear at least twice
        #  in the log; teardown_c appears once from the finally block.)
        assert call_log.index("teardown_b") < call_log.index("teardown_a")

    async def test_validate_encrypted_config_rows_called_before_init_hooks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate_encrypted_config_rows must fire before any custom_init_tasks."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def fake_validate(pool) -> None:
            call_log.append("validate")

        async def init_hook(app: FastAPI) -> None:
            call_log.append("init_hook")

        config = ServiceLifespanConfig(
            service_name="test_ordering",
            jobs_worker_kinds=set(),
            custom_init_tasks=[init_hook],
            custom_teardown_tasks=[None],  # no teardown needed; pad with None
        )

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
            app = FastAPI()
            async with configure_lifespan(config)(app):
                pass

        # validate must appear before init_hook in the call log
        assert call_log.index("validate") < call_log.index("init_hook")


# ---------------------------------------------------------------------------
# NI-5: equal-length init/teardown contract
# ---------------------------------------------------------------------------


class TestEqualLengthContract:
    async def test_configure_lifespan_raises_on_mismatched_init_teardown_lengths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ValueError raised when custom_init_tasks and custom_teardown_tasks differ in length."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        async def init_a(app: FastAPI) -> None:
            pass

        async def init_b(app: FastAPI) -> None:
            pass

        async def teardown_a(app: FastAPI) -> None:
            pass

        config = ServiceLifespanConfig(
            service_name="test_mismatch",
            jobs_worker_kinds=set(),
            custom_init_tasks=[init_a, init_b],
            custom_teardown_tasks=[teardown_a],  # only 1 teardown for 2 inits
        )

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with _lifespan_stack(fake_pool, fake_http_client):
            app = FastAPI()
            lifespan = configure_lifespan(config)
            with pytest.raises(ValueError, match="must have equal length"):
                async with lifespan(app):
                    pass  # pragma: no cover

    async def test_configure_lifespan_accepts_none_padded_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None is a valid teardown placeholder; no exception raised and teardown_map allows None."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_a(app: FastAPI) -> None:
            call_log.append("init_a")

        async def init_b(app: FastAPI) -> None:
            call_log.append("init_b")

        async def teardown_a(app: FastAPI) -> None:
            call_log.append("teardown_a")

        config = ServiceLifespanConfig(
            service_name="test_none_pad",
            jobs_worker_kinds=set(),
            custom_init_tasks=[init_a, init_b],
            custom_teardown_tasks=[teardown_a, None],  # init_b has no teardown
        )

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with _lifespan_stack(fake_pool, fake_http_client):
            app = FastAPI()
            # Should not raise
            async with configure_lifespan(config)(app):
                pass

        assert "init_a" in call_log
        assert "init_b" in call_log
        # teardown_a runs in the finally block
        assert "teardown_a" in call_log
