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
        # Compensating teardown must have run for init_first, exactly once
        # (AsyncExitStack eliminates the old double-execution bug).
        assert "init_first" in call_log
        assert "init_second" in call_log
        assert "teardown_first" in call_log
        assert call_log.count("teardown_first") == 1, "teardown_first must run exactly once"

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

        # init_a and init_b completed; init_c raised (so its teardown was never
        # registered).  AsyncExitStack runs teardowns LIFO: teardown_b before
        # teardown_a.  Each must appear exactly once — the old double-execution
        # bug (compensating path + finally block both running teardowns) is gone.
        assert call_log.index("teardown_b") < call_log.index("teardown_a")
        assert call_log.count("teardown_a") == 1, "teardown_a must run exactly once"
        assert call_log.count("teardown_b") == 1, "teardown_b must run exactly once"
        # teardown_c must NOT run: init_c never succeeded so it was never pushed.
        assert "teardown_c" not in call_log, "teardown_c must not run when init_c failed"

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
# W1-23: AsyncExitStack — partial startup → partial teardown, no double-exec
# ---------------------------------------------------------------------------


class TestAsyncExitStackPartialStartup:
    """Adversarial tests verifying AsyncExitStack fixes the double-execution bug.

    Regression guard: these tests MUST fail if the old try/finally pattern
    (compensating path + unconditional finally loop) is restored, because that
    pattern runs completed teardowns twice.
    """

    async def test_partial_startup_runs_only_completed_teardowns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the N-th init hook fails, only hooks 1..N-1 teardowns run, each exactly once.

        This is the canonical regression test for W1-23.  The old code had a
        double-execution bug: the compensating path ran teardowns for completed
        hooks, then the ``finally`` block ran ALL teardowns again
        unconditionally.  With ``AsyncExitStack`` each teardown is registered
        once and runs once.
        """
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_1(app: FastAPI) -> None:
            call_log.append("init_1")

        async def init_2(app: FastAPI) -> None:
            call_log.append("init_2")

        async def init_3(app: FastAPI) -> None:
            call_log.append("init_3")
            raise RuntimeError("init_3 boom — partial startup")

        async def teardown_1(app: FastAPI) -> None:
            call_log.append("teardown_1")

        async def teardown_2(app: FastAPI) -> None:
            call_log.append("teardown_2")

        async def teardown_3(app: FastAPI) -> None:
            call_log.append("teardown_3")  # pragma: no cover — init_3 never succeeded

        config = ServiceLifespanConfig(
            service_name="test_partial_startup",
            custom_init_tasks=[init_1, init_2, init_3],
            custom_teardown_tasks=[teardown_1, teardown_2, teardown_3],
        )

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with _lifespan_stack(fake_pool, fake_http_client):
            app = FastAPI()
            with pytest.raises(RuntimeError, match="init_3 boom"):
                async with configure_lifespan(config)(app):
                    pass  # pragma: no cover

        # init_1 and init_2 completed; init_3 raised.
        assert "init_1" in call_log
        assert "init_2" in call_log
        assert "init_3" in call_log

        # teardown_1 and teardown_2 must each run exactly once (LIFO order).
        assert call_log.count("teardown_1") == 1, "teardown_1 double-executed (regression)"
        assert call_log.count("teardown_2") == 1, "teardown_2 double-executed (regression)"

        # teardown_3 must NOT run at all — init_3 never succeeded.
        assert "teardown_3" not in call_log, "teardown_3 ran despite init_3 failing"

        # LIFO order: teardown_2 ran before teardown_1.
        assert call_log.index("teardown_2") < call_log.index("teardown_1")

        # Built-in resources cleaned up exactly once.
        fake_pool.close.assert_awaited_once()
        fake_http_client.aclose.assert_awaited_once()

    async def test_successful_startup_teardowns_run_lifo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On clean shutdown, teardowns run in LIFO (reverse-init) order, each exactly once."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_x(app: FastAPI) -> None:
            call_log.append("init_x")

        async def init_y(app: FastAPI) -> None:
            call_log.append("init_y")

        async def init_z(app: FastAPI) -> None:
            call_log.append("init_z")

        async def teardown_x(app: FastAPI) -> None:
            call_log.append("teardown_x")

        async def teardown_y(app: FastAPI) -> None:
            call_log.append("teardown_y")

        async def teardown_z(app: FastAPI) -> None:
            call_log.append("teardown_z")

        config = ServiceLifespanConfig(
            service_name="test_lifo_shutdown",
            custom_init_tasks=[init_x, init_y, init_z],
            custom_teardown_tasks=[teardown_x, teardown_y, teardown_z],
        )

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with _lifespan_stack(fake_pool, fake_http_client):
            app = FastAPI()
            async with configure_lifespan(config)(app):
                assert call_log == ["init_x", "init_y", "init_z"]

        # Each teardown appears exactly once.
        assert call_log.count("teardown_x") == 1
        assert call_log.count("teardown_y") == 1
        assert call_log.count("teardown_z") == 1

        # LIFO: z before y before x.
        teardown_sequence = [e for e in call_log if e.startswith("teardown_")]
        assert teardown_sequence == ["teardown_z", "teardown_y", "teardown_x"]


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
