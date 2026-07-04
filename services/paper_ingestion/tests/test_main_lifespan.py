"""Lifespan tests for paper_ingestion — B.4 Step 4 procrastinate worker wiring.

Covers the ``_start_procrastinate_worker`` /
``_shutdown_procrastinate_worker`` hooks added in Task B.2:

- the procrastinate worker is created as a named asyncio.Task during startup
- ``set_dependencies`` is called with the lifespan-owned pool + http_client
- on shutdown the worker task is cancelled cleanly without an unawaited
  coroutine warning, and the procrastinate connector is closed.

We don't drive the full real lifespan (real run_migrations, telegram
bootstrap, scheduler init, etc. all need live DBs/HTTP) — instead we
construct a minimal ``ServiceLifespanConfig`` that exercises ONLY the
broker hook and assert the documented post-conditions. The full main.py
config is checked by structural assertions on the hook list.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_reconciler_log_state():
    """The reconciler's transition-only logging keeps module-level state
    (failure streaks + once-per-value seen-sets) — clear it per test so the
    first-failure / first-anomaly log assertions are deterministic."""
    from paper_ingestion import litellm_reconciler

    litellm_reconciler._RECONCILE_FAILURE_STREAKS.clear()
    litellm_reconciler._ALIAS_PLACEHOLDER_LOGGED.clear()
    litellm_reconciler._EMBED_MISMATCH_WARNED.clear()
    yield
    litellm_reconciler._RECONCILE_FAILURE_STREAKS.clear()
    litellm_reconciler._ALIAS_PLACEHOLDER_LOGGED.clear()
    litellm_reconciler._EMBED_MISMATCH_WARNED.clear()


@pytest.fixture()
def fake_pool() -> AsyncMock:
    pool = AsyncMock()
    pool.close = AsyncMock()
    return pool


@pytest.fixture()
def fake_http_client() -> AsyncMock:
    client = AsyncMock()
    client.aclose = AsyncMock()
    return client


def _patch_factory_io(fake_pool: AsyncMock, fake_http_client: AsyncMock) -> list[Any]:
    # Use patch.object on the httpx module as bound in app_factory so that the
    # patch target survives any future refactor of the import alias (D5-08).
    import jarvis_common.app_factory as _af

    return [
        patch(
            "jarvis_common.app_factory.asyncpg.create_pool",
            AsyncMock(return_value=fake_pool),
        ),
        patch(
            "jarvis_common.app_factory.validate_encrypted_config_rows",
            AsyncMock(return_value=None),
        ),
        patch(
            "jarvis_common.app_factory.validate_production_config",
            MagicMock(return_value=None),
        ),
        patch.object(
            _af.httpx,
            "AsyncClient",
            MagicMock(return_value=fake_http_client),
        ),
    ]


# ---------------------------------------------------------------------------
# Structural assertions on the real _lifespan_config
# ---------------------------------------------------------------------------


def test_lifespan_config_includes_procrastinate_hooks() -> None:
    """The real main.py config must wire both the start + shutdown hook."""
    from paper_ingestion.main import (
        _lifespan_config,
        _shutdown_procrastinate_worker,
        _start_procrastinate_worker,
    )

    assert _start_procrastinate_worker in _lifespan_config.custom_init_tasks
    init_idx = _lifespan_config.custom_init_tasks.index(_start_procrastinate_worker)
    # Same index in teardown list = compensating teardown wiring.
    assert _lifespan_config.custom_teardown_tasks[init_idx] is _shutdown_procrastinate_worker


# ---------------------------------------------------------------------------
# Behavioural test of the broker hook itself, via a minimal lifespan
# ---------------------------------------------------------------------------


class TestProcrastinateWorkerLifespan:
    async def test_start_and_shutdown_hooks_drive_worker_lifecycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_pool: AsyncMock,
        fake_http_client: AsyncMock,
    ) -> None:
        """Start hook spawns named worker on the right queues; shutdown hook cancels cleanly."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        worker_started = asyncio.Event()
        worker_cancelled = asyncio.Event()
        captured_kwargs: dict[str, Any] = {}

        async def fake_run_worker_async(**kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            worker_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise

        fake_proc_app = MagicMock()
        fake_proc_app.run_worker_async = fake_run_worker_async
        fake_proc_app.open_async = AsyncMock(return_value=None)
        fake_proc_app.close_async = AsyncMock(return_value=None)
        fake_proc_app.connector = MagicMock()
        fake_proc_app.job_manager = MagicMock()

        set_dependencies_mock = MagicMock()

        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan
        from paper_ingestion.main import (
            _shutdown_procrastinate_worker,
            _start_procrastinate_worker,
        )

        # Minimal config: just the two procrastinate hooks and no other init noise.
        minimal_config = ServiceLifespanConfig(
            service_name="test_paper_ingestion_lifespan",
            custom_init_tasks=[_start_procrastinate_worker],
            custom_teardown_tasks=[_shutdown_procrastinate_worker],
        )

        with contextlib.ExitStack() as stack:
            for p in _patch_factory_io(fake_pool, fake_http_client):
                stack.enter_context(p)
            stack.enter_context(patch("jarvis_common.task_registry.app", fake_proc_app))
            stack.enter_context(
                patch(
                    "jarvis_common.task_registry.set_dependencies",
                    set_dependencies_mock,
                )
            )
            # register_paper_ingestion_tasks uses @procrastinate_app.task decorator
            # which doesn't work on a MagicMock — stub it out so lifespan can proceed.
            stack.enter_context(
                patch(
                    "paper_ingestion._task_register.register_paper_ingestion_tasks",
                    MagicMock(),
                )
            )

            from fastapi import FastAPI

            app = FastAPI()
            lifespan = configure_lifespan(minimal_config)
            async with lifespan(app):
                await asyncio.wait_for(worker_started.wait(), timeout=2.0)

                # Worker was started with the paper_ingestion-specific queues +
                # builtin (so procrastinate's auto-registered remove_old_jobs
                # cleanup task runs out of this service).
                assert list(captured_kwargs.get("queues", [])) == [
                    "paper_ingestion",
                    "builtin",
                ]
                # Signal handlers stay off — we manage cancellation from the
                # lifespan, not from SIGINT/SIGTERM in the worker.
                assert captured_kwargs.get("install_signal_handlers") is False

                # set_dependencies received the lifespan-owned pool + http_client
                # (the ones bound to app.state by configure_lifespan).
                set_dependencies_mock.assert_called_once_with(
                    app.state.db_pool, app.state.http_client
                )
                assert app.state.db_pool is fake_pool
                assert app.state.http_client is fake_http_client

                # Worker task is recorded on app.state with the expected name.
                worker_task = app.state.procrastinate_worker_task
                assert isinstance(worker_task, asyncio.Task)
                assert worker_task.get_name() == "procrastinate_worker"
                assert not worker_task.done()

                # Connector was opened.
                fake_proc_app.open_async.assert_awaited_once()

            # On exit the procrastinate worker was cancelled cleanly and the
            # connector was closed.
            assert worker_cancelled.is_set()
            assert app.state.procrastinate_worker_task.done()
            fake_proc_app.close_async.assert_awaited()


# ---------------------------------------------------------------------------
# Boot gate: multi-user non-prod boot requires JARVIS_MODEL_HMAC_KEY
# ---------------------------------------------------------------------------


class TestModelHmacKeyBootGate:
    """validate_production_config runs first in configure_lifespan (before the
    DB pool), so a multi-user (JARVIS_SETUP_MODE != single) boot without a
    dedicated JARVIS_MODEL_HMAC_KEY must abort startup — defending the pulse
    pickle-signing path from a stolen-bearer forgery on internal multi-user
    boxes. Single-user dev is unchanged."""

    @staticmethod
    def _minimal_lifespan():
        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

        config = ServiceLifespanConfig(service_name="test_hmac_boot_gate")
        return configure_lifespan(config)

    async def test_multi_user_nonprod_boot_requires_hmac_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import FastAPI

        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("DEV_MODE", "false")
        monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
        monkeypatch.setenv("JARVIS_SETUP_MODE", "multi")
        monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY", raising=False)
        monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY_FILE", raising=False)

        lifespan = self._minimal_lifespan()
        # The gate raises before any DB pool is created, so no asyncpg patch is needed.
        with pytest.raises(RuntimeError, match="JARVIS_MODEL_HMAC_KEY"):
            async with lifespan(FastAPI()):
                pass

    async def test_single_user_dev_boot_does_not_require_hmac_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_pool: AsyncMock,
        fake_http_client: AsyncMock,
    ) -> None:
        from fastapi import FastAPI

        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("DEV_MODE", "false")
        monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
        monkeypatch.setenv("JARVIS_SETUP_MODE", "single")
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY", raising=False)
        monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY_FILE", raising=False)

        import jarvis_common.app_factory as _af

        lifespan = self._minimal_lifespan()
        # Single-user dev must pass the boot gate; the REAL
        # validate_production_config runs (not stubbed) so the gate is genuinely
        # exercised. Only the post-gate DB/HTTP I/O is stubbed out.
        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool",
                AsyncMock(return_value=fake_pool),
            ),
            patch(
                "jarvis_common.app_factory.validate_encrypted_config_rows",
                AsyncMock(return_value=None),
            ),
            patch.object(
                _af.httpx,
                "AsyncClient",
                MagicMock(return_value=fake_http_client),
            ),
        ):
            async with lifespan(FastAPI()) as _:
                pass


# ---------------------------------------------------------------------------
# _autoconfigure_models_hook structural + unit tests
# ---------------------------------------------------------------------------


def test_lifespan_config_includes_autoconfigure_hook() -> None:
    """_autoconfigure_models_hook must be registered and have a None teardown counterpart."""
    from paper_ingestion.main import _autoconfigure_models_hook, _lifespan_config

    assert _autoconfigure_models_hook in _lifespan_config.custom_init_tasks
    idx = _lifespan_config.custom_init_tasks.index(_autoconfigure_models_hook)
    assert _lifespan_config.custom_teardown_tasks[idx] is None


@pytest.mark.asyncio
async def test_autoconfigure_models_hook_sets_flag_and_writes_user_config() -> None:
    """On first boot, the hook detects tier and writes llm.* + autoconfigured flag."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from paper_ingestion.main import _autoconfigure_models_hook
    from paper_ingestion.services.model_lifecycle import HardwareInfo

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    # No user_config rows exist yet (first boot) — all fetchrow calls return None.
    conn.fetchrow.return_value = None

    tier1_hw = HardwareInfo(
        vram_gb=8.0, vram_source="nvidia-smi", tier=1, detected_at="2026-05-06T00:00:00+00:00"
    )

    app = FastAPI()
    app.state.db_pool = pool

    with (
        patch(
            "paper_ingestion.services.model_lifecycle.detect_hardware",
            return_value=tier1_hw,
        ),
        patch(
            "paper_ingestion.services.model_lifecycle.recommendations_for_role",
            return_value=[{"id": "qwen3:4b", "status": "downloadable", "tier": 1}],
        ),
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _autoconfigure_models_hook(app)

    # At least 3 INSERT calls: smart + fast roles + flag (embed is not
    # auto-configured — it is dimension-locked to the Qdrant collection).
    execute_calls = [str(call) for call in conn.execute.await_args_list]
    insert_calls = [c for c in execute_calls if "INSERT INTO user_config" in c]
    assert len(insert_calls) >= 3  # 2 roles + autoconfigured flag


@pytest.mark.asyncio
async def test_autoconfigure_host_gpu_divergence_seeds_conservatively() -> None:
    """FX3: when host_gpu_divergence is True (host VRAM env set but the
    in-container probe sees no GPU), the hook must seed off the conservative
    vram=0 path — smallest-first model + catalog-default num_ctx — NOT off the
    phantom host VRAM. With installed {qwen3:4b, qwen3:8b} on a (phantom) 24 GB
    box, smart must be qwen3:4b (smallest-first) and its num_ctx must be the
    catalog default (8192), never an oversized 24 GB-derived stop.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from paper_ingestion.main import _autoconfigure_models_hook
    from paper_ingestion.services.model_lifecycle import HardwareInfo, catalog_entry_for_model

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    conn.fetchrow.return_value = None  # first boot

    diverged_hw = HardwareInfo(
        vram_gb=24.0,
        vram_source="host-env",
        tier=3,
        detected_at="2026-06-12T00:00:00+00:00",
        machine_id="test",
        host_gpu_divergence=True,
    )

    class _TagsClient:
        async def get(self, url, **kwargs):
            payload = {
                "models": [
                    {"name": "qwen3:4b", "size": 1, "details": {}},
                    {"name": "qwen3:8b", "size": 1, "details": {}},
                ]
            }
            return SimpleNamespace(
                status_code=200, raise_for_status=lambda: None, json=lambda: payload
            )

    app = FastAPI()
    app.state.db_pool = pool
    app.state.http_client = _TagsClient()

    with (
        patch("paper_ingestion.main.detect_hardware", return_value=diverged_hw),
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _autoconfigure_models_hook(app)

    # Collect (key, value) for each llm.* INSERT.
    llm_inserts = {
        c.args[1]: c.args[2]
        for c in conn.execute.await_args_list
        if len(c.args) >= 3 and isinstance(c.args[1], str) and c.args[1].startswith("llm.")
    }

    # Conservative smallest-first: smart=4b (NOT 8b, which the 24 GB path would pick).
    assert llm_inserts.get("llm.smart_model") == "qwen3:4b"
    assert llm_inserts.get("llm.fast_model") == "qwen3:4b"

    # num_ctx is the catalog default for the chosen model, never an oversized stop.
    qwen4b_default = catalog_entry_for_model("qwen3:4b").default_num_ctx
    assert llm_inserts.get("llm.test.smart_num_ctx") == qwen4b_default
    assert llm_inserts.get("llm.test.fast_num_ctx") == qwen4b_default


@pytest.mark.asyncio
async def test_autoconfigure_models_hook_stores_bare_string_models_and_int_num_ctx() -> None:
    """Regression: ``llm.*_model`` rows store a bare Python str (not json.dumps(str)),
    and ``llm.<machine>.<role>_num_ctx`` rows store a bare Python int.

    asyncpg's JSONB codec (registered in init_pg_connection) already calls
    json.dumps on the value before sending it to Postgres.  Wrapping a model_id
    in json.dumps() a second time would produce ``'"qwen3:4b"'`` in the DB
    (a JSON-encoded JSON string) instead of the bare string ``"qwen3:4b"``.

    Widened intent: the hook now also seeds per-machine num_ctx
    rows alongside each model. Those carry an int value — assert the model rows
    stay bare strings AND the num_ctx rows stay bare ints (neither stringified
    nor double-encoded JSON).
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from paper_ingestion.main import _autoconfigure_models_hook
    from paper_ingestion.services.model_lifecycle import HardwareInfo

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    conn.fetchrow.return_value = None  # first boot

    tier1_hw = HardwareInfo(
        vram_gb=8.0,
        vram_source="nvidia-smi",
        tier=1,
        detected_at="2026-05-14T00:00:00+00:00",
        machine_id="test",
    )

    app = FastAPI()
    app.state.db_pool = pool

    with (
        patch(
            "paper_ingestion.services.model_lifecycle.detect_hardware",
            return_value=tier1_hw,
        ),
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _autoconfigure_models_hook(app)

    # Collect every INSERT INTO user_config call that sets an llm.* key.
    llm_inserts = [
        c
        for c in conn.execute.await_args_list
        if len(c.args) >= 3 and isinstance(c.args[1], str) and c.args[1].startswith("llm.")
    ]
    assert llm_inserts, "Expected at least one INSERT for llm.* keys"

    model_inserts = [c for c in llm_inserts if c.args[1].endswith("_model")]
    num_ctx_inserts = [c for c in llm_inserts if c.args[1].endswith("_num_ctx")]
    assert model_inserts, "Expected llm.*_model INSERT(s)"
    assert num_ctx_inserts, "Expected llm.<machine>.<role>_num_ctx INSERT(s)"

    for c in model_inserts:
        # args[2] is the value parameter ($2::jsonb).
        value_arg = c.args[2]
        # Must be a plain Python str — NOT the result of json.dumps(str)  # nolint:jsonb-double-encode
        # which would look like '"qwen3:4b"' (starts and ends with a quote).
        assert isinstance(value_arg, str), f"Expected str for {c.args[1]}, got {type(value_arg)}"
        assert not (value_arg.startswith('"') and value_arg.endswith('"')), (
            f"Value {value_arg!r} looks double-encoded (json.dumps was applied "
            "before passing to asyncpg). Pass model_id directly."
        )

    for c in num_ctx_inserts:
        value_arg = c.args[2]
        # bool is an int subclass — exclude it so a stray True/False is caught.
        assert isinstance(value_arg, int) and not isinstance(value_arg, bool), (
            f"Expected bare int for {c.args[1]}, got {type(value_arg)}: {value_arg!r}"
        )


@pytest.mark.asyncio
async def test_autoconfigure_models_hook_is_idempotent() -> None:
    """When flag is already set, the hook returns early without writing anything.

    The hook no longer delivers to LiteLLM at all (the boot reconciler owns
    delivery), so its only DB read is the autoconfigured flag itself.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from paper_ingestion.main import _autoconfigure_models_hook

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    # system.models_autoconfigured → already set
    conn.fetchrow.return_value = {"value": "true"}

    app = FastAPI()
    app.state.db_pool = pool

    await _autoconfigure_models_hook(app)

    # No INSERT should have been executed (idempotent early-return).
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# LiteLLM model reconciler — structural + behavioral
# ---------------------------------------------------------------------------


def _make_pool() -> tuple[MagicMock, AsyncMock]:
    """Pool-shaped mock whose acquire() yields a single AsyncMock connection."""
    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


def test_lifespan_config_includes_litellm_reconciler_hooks() -> None:
    """The reconciler start hook is wired with a paired teardown, after autoconfigure.

    Ordering matters: the reconciler reads the llm.* rows that
    _autoconfigure_models_hook writes on first boot.
    """
    from paper_ingestion.main import (
        _autoconfigure_models_hook,
        _lifespan_config,
        _shutdown_litellm_reconciler,
        _start_litellm_reconciler,
    )

    assert _start_litellm_reconciler in _lifespan_config.custom_init_tasks
    idx = _lifespan_config.custom_init_tasks.index(_start_litellm_reconciler)
    # Same index in teardown list = compensating teardown wiring.
    assert _lifespan_config.custom_teardown_tasks[idx] is _shutdown_litellm_reconciler
    assert _lifespan_config.custom_init_tasks.index(_autoconfigure_models_hook) < idx


@pytest.mark.asyncio
async def test_reconcile_no_db_marks_pending_and_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A LiteLLM "No DB Connected" failure is surfaced, never silently skipped.

    Inversion of the old rehydrate semantics: a DB-less proxy (prisma migrate
    failed) is a degraded state the reconciler keeps retrying — each affected
    role is marked pending (UI pill) and the pass reports failure so the loop
    runs again. It must NOT raise (boot survives LiteLLM warming up).
    """
    import logging

    from paper_ingestion.main import _reconcile_litellm_models_once

    pool, conn = _make_pool()
    conn.fetchrow.return_value = {"value": "qwen3:4b"}

    no_db_exc = RuntimeError(
        "LiteLLM /model/new failed for alias 'smart': "
        'HTTP 500 {"error": "No DB Connected. Here\'s how to do it - ..."}'
    )

    mock_pending = AsyncMock()
    mock_fallback = AsyncMock()
    with (
        caplog.at_level(logging.WARNING, logger="paper_ingestion.main"),
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(side_effect=no_db_exc),
        ),
        patch(
            "paper_ingestion.services.litellm_config.ensure_smart_fallback",
            new=mock_fallback,
        ),
        patch(
            "paper_ingestion.services.config_write._update_delivery_pending_roles",
            new=mock_pending,
        ),
    ):
        # Must NOT raise.
        result = await _reconcile_litellm_models_once(pool)

    assert result is False, "a failed delivery must report the pass as failed (loop retries)"

    # Every undelivered role is marked pending — never a phantom "applied".
    # (embed is dimension-locked and never auto-delivered, so no marker for it.)
    recorded = [(c.kwargs["roles"], c.kwargs["pending"]) for c in mock_pending.await_args_list]
    assert recorded == [({"smart"}, True), ({"fast"}, True)], (
        f"Expected smart+fast marked pending; got: {recorded}"
    )

    # smart-fallback is not attempted while the roles are undelivered.
    mock_fallback.assert_not_awaited()

    assert any(
        "will retry" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), f"Expected WARNING log about retrying; got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_reconcile_success_clears_pending_and_creates_fallback() -> None:
    """A successful pass clears pending markers and ensures smart-fallback.

    Covers both success shapes: True (deployment replaced) AND False ("nothing
    to change" — the alias already routes that model). Either way LiteLLM
    routes the committed model, so any stale marker from a pre-restart "No DB
    Connected" commit must be cleared (restart-after-DB-attach recovery).
    """
    from paper_ingestion.main import _reconcile_litellm_models_once

    pool, conn = _make_pool()
    conn.fetchrow.side_effect = [
        {"value": "qwen3:8b"},  # llm.smart_model — update returns True
        {"value": "qwen3:4b"},  # llm.fast_model  — update returns False (already routed)
        None,  # llm.embed_model — no row: nothing to warn about
    ]

    mock_update = AsyncMock(side_effect=[True, False])
    mock_fallback = AsyncMock(return_value=True)
    mock_pending = AsyncMock()
    with (
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=mock_update,
        ),
        patch(
            "paper_ingestion.services.litellm_config.ensure_smart_fallback",
            new=mock_fallback,
        ),
        patch(
            "paper_ingestion.services.config_write._update_delivery_pending_roles",
            new=mock_pending,
        ),
    ):
        result = await _reconcile_litellm_models_once(pool)

    assert result is True

    recorded = [(c.kwargs["roles"], c.kwargs["pending"]) for c in mock_pending.await_args_list]
    assert recorded == [({"smart"}, False), ({"fast"}, False)], (
        f"Expected smart+fast cleared (False return included); got: {recorded}"
    )

    delivered = [c.args[:2] for c in mock_update.await_args_list]
    assert delivered == [("llm.smart_model", "qwen3:8b"), ("llm.fast_model", "qwen3:4b")]

    # The smart-fallback deployment group is created from the fast-tier model.
    assert mock_fallback.await_args is not None
    assert mock_fallback.await_args.args[0] == "qwen3:4b"


@pytest.mark.asyncio
async def test_reconcile_ignores_bare_alias_and_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored bare alias ("smart") is never forwarded — the .env fallback wins.

    Defense-in-depth: a stray re-seed could write "smart"/"fast"/"embed" back
    into user_config. Forwarding "smart" to LiteLLM would create
    ``ollama/smart`` → 404, so the reconciler falls back to the setup-chosen
    .env model (JARVIS_SMART_MODEL) instead.
    """
    import logging

    from paper_ingestion.main import _reconcile_litellm_models_once

    monkeypatch.setenv("JARVIS_SMART_MODEL", "qwen3:14b")
    pool, conn = _make_pool()
    conn.fetchrow.side_effect = [
        {"value": "smart"},  # llm.smart_model — bare alias placeholder
        {"value": "qwen3:4b"},  # llm.fast_model  — real model
        None,  # llm.embed_model — not set
    ]

    mock_update = AsyncMock(return_value=True)
    with (
        caplog.at_level(logging.INFO, logger="paper_ingestion.main"),
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=mock_update,
        ),
        patch(
            "paper_ingestion.services.litellm_config.ensure_smart_fallback",
            new=AsyncMock(return_value=False),
        ),
        # Pending bookkeeping is not under test here — stub it so the real
        # helper never runs SQL against the AsyncMock connection.
        patch(
            "paper_ingestion.services.config_write._update_delivery_pending_roles",
            new=AsyncMock(),
        ),
    ):
        result = await _reconcile_litellm_models_once(pool)

    assert result is True
    delivered = {c.args[0]: c.args[1] for c in mock_update.await_args_list}
    assert delivered == {"llm.smart_model": "qwen3:14b", "llm.fast_model": "qwen3:4b"}, (
        f"Expected env fallback for smart + stored model for fast; got: {delivered}"
    )

    # An INFO log must mention the ignored key.
    assert any(
        "llm.smart_model" in record.message and "alias placeholder" in record.message
        for record in caplog.records
        if record.levelno == logging.INFO
    ), f"Expected INFO log about alias-placeholder skip; got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_reconcile_missing_rows_use_env_then_static_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stored rows: the .env value wins when set, else the static pulled default."""
    from paper_ingestion.main import _reconcile_litellm_models_once

    monkeypatch.setenv("JARVIS_SMART_MODEL", "qwen3:30b-a3b")
    monkeypatch.delenv("JARVIS_FAST_MODEL", raising=False)
    pool, conn = _make_pool()
    conn.fetchrow.return_value = None

    mock_update = AsyncMock(return_value=True)
    with (
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=mock_update,
        ),
        patch(
            "paper_ingestion.services.litellm_config.ensure_smart_fallback",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "paper_ingestion.services.config_write._update_delivery_pending_roles",
            new=AsyncMock(),
        ),
    ):
        result = await _reconcile_litellm_models_once(pool)

    assert result is True
    delivered = {c.args[0]: c.args[1] for c in mock_update.await_args_list}
    assert delivered == {"llm.smart_model": "qwen3:30b-a3b", "llm.fast_model": "qwen3:4b"}


# ---------------------------------------------------------------------------
# Production path: cached singletons must carry db_pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_source_singletons_passes_db_pool_to_ctor() -> None:
    """Regression: _init_source_singletons must pass db_pool= to the source ctor.

    This guards the PRODUCTION pulse path: discover_candidates is called with
    source_cache=services.sources (pre-built singletons), so any db_pool omission
    in _init_source_singletons silently makes PersistentSourceRateLimiter inert
    across all real pulse runs.  If this test turns red, this invariant has regressed.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from paper_ingestion.main import _init_source_singletons
    from paper_ingestion.models import SourceType

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    # Return a valid DB row for every source type queried.
    conn.fetchrow.return_value = {
        "id": 1,
        "source_type": "arxiv",
        "enabled": True,
        "config": {},
    }

    received_db_pools: dict[str, object] = {}

    def _make_spy_class(source_type_val: str):
        class SpySource:
            def __init__(self, config, http_client, db_pool=None):
                received_db_pools[source_type_val] = db_pool

        return SpySource

    http_client = MagicMock()
    app = FastAPI()
    app.state.db_pool = pool
    app.state.http_client = http_client

    preloaded = [
        SourceType.ARXIV,
        SourceType.SEMANTIC_SCHOLAR,
        SourceType.PUBMED,
        SourceType.OPENALEX,
    ]

    def _spy_get_source_class(source_type_val: str):
        return _make_spy_class(source_type_val)

    with (
        patch(
            "paper_ingestion.main.get_source_class",
            side_effect=_spy_get_source_class,
        ),
        patch("paper_ingestion._state.set_services"),
    ):
        await _init_source_singletons(app)

    for st in preloaded:
        assert st.value in received_db_pools, (
            f"source '{st.value}' was never constructed — check preloaded_sources list"
        )
        assert received_db_pools[st.value] is pool, (
            f"source '{st.value}' singleton was built without db_pool; "
            "PersistentSourceRateLimiter is inert on the production pulse path (regression)"
        )


@pytest.mark.asyncio
async def test_reconcile_transport_error_marks_pending_not_raises() -> None:
    """ANY delivery failure (e.g. HTTP 502) is caught, marked pending, retried.

    Inversion of the old rehydrate reraise-502 semantics: the reconciler owns
    retries now, so a transient gateway error must not crash the pass (or the
    lifespan) — it marks the roles pending and reports failure so the loop
    runs again.
    """
    from paper_ingestion.main import _reconcile_litellm_models_once

    pool, conn = _make_pool()
    conn.fetchrow.return_value = {"value": "qwen3:4b"}

    gateway_exc = RuntimeError("LiteLLM /model/new failed for alias 'smart': HTTP 502 Bad Gateway")

    mock_pending = AsyncMock()
    with (
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(side_effect=gateway_exc),
        ),
        patch(
            "paper_ingestion.services.litellm_config.ensure_smart_fallback",
            new=AsyncMock(),
        ),
        patch(
            "paper_ingestion.services.config_write._update_delivery_pending_roles",
            new=mock_pending,
        ),
    ):
        # Must NOT raise.
        result = await _reconcile_litellm_models_once(pool)

    assert result is False
    recorded = [(c.kwargs["roles"], c.kwargs["pending"]) for c in mock_pending.await_args_list]
    assert recorded == [({"smart"}, True), ({"fast"}, True)]


def _reconcile_patches(update_mock: AsyncMock, fallback_mock: AsyncMock):
    """The three patches every reconcile-pass test needs (update/fallback/pending)."""
    return (
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=update_mock,
        ),
        patch(
            "paper_ingestion.services.litellm_config.ensure_smart_fallback",
            new=fallback_mock,
        ),
        patch(
            "paper_ingestion.services.config_write._update_delivery_pending_roles",
            new=AsyncMock(),
        ),
    )


@pytest.mark.asyncio
async def test_reconcile_failure_warning_is_transition_gated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Delivery-failure WARNINGs fire on the failure TRANSITION, not every pass.

    A LiteLLM degraded for hours would otherwise emit a full traceback per role
    every 30 s. Contract: first consecutive failure per role logs the full
    WARNING+traceback; repeat failures stay silent (until the terse Nth-pass
    heartbeat); a success resets the streak so the next outage logs fully again.
    """
    import logging

    from paper_ingestion.main import _reconcile_litellm_models_once

    pool, conn = _make_pool()
    conn.fetchrow.return_value = None  # roles fall back to static defaults; no embed row

    def _full_warnings(key: str) -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and f"could not deliver {key}" in r.message
        ]

    failing = AsyncMock(side_effect=RuntimeError("HTTP 502 Bad Gateway"))
    with caplog.at_level(logging.WARNING, logger="paper_ingestion.main"):
        with contextlib.ExitStack() as stack:
            for p in _reconcile_patches(failing, AsyncMock()):
                stack.enter_context(p)
            assert await _reconcile_litellm_models_once(pool) is False
            assert await _reconcile_litellm_models_once(pool) is False

        # Two failing passes → exactly ONE full warning per role, traceback attached.
        for key in ("llm.smart_model", "llm.fast_model"):
            records = _full_warnings(key)
            assert len(records) == 1, f"expected one transition warning for {key}"
            assert records[0].exc_info is not None

        # A successful pass resets the streak...
        with contextlib.ExitStack() as stack:
            for p in _reconcile_patches(
                AsyncMock(return_value=True), AsyncMock(return_value=False)
            ):
                stack.enter_context(p)
            assert await _reconcile_litellm_models_once(pool) is True

        # ...so the next outage is a fresh transition and logs fully again.
        with contextlib.ExitStack() as stack:
            for p in _reconcile_patches(failing, AsyncMock()):
                stack.enter_context(p)
            assert await _reconcile_litellm_models_once(pool) is False
        assert len(_full_warnings("llm.smart_model")) == 2


@pytest.mark.asyncio
async def test_reconcile_persistent_failure_logs_terse_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """While a failure persists, a terse no-traceback WARNING fires every Nth pass."""
    import logging

    from paper_ingestion import litellm_reconciler
    from paper_ingestion.main import _reconcile_litellm_models_once

    monkeypatch.setattr(litellm_reconciler, "_RECONCILE_TERSE_EVERY_N", 2)
    pool, conn = _make_pool()
    conn.fetchrow.return_value = None

    failing = AsyncMock(side_effect=RuntimeError("HTTP 502 Bad Gateway"))
    with caplog.at_level(logging.WARNING, logger="paper_ingestion.main"):
        with contextlib.ExitStack() as stack:
            for p in _reconcile_patches(failing, AsyncMock()):
                stack.enter_context(p)
            for _ in range(3):  # streaks 0 (full), 1 (silent), 2 (terse heartbeat)
                await _reconcile_litellm_models_once(pool)

    terse = [r for r in caplog.records if "still cannot deliver llm.smart_model" in r.message]
    assert len(terse) == 1
    assert terse[0].exc_info is None  # terse heartbeat carries no traceback


@pytest.mark.asyncio
async def test_reconcile_bare_alias_logged_once_per_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bare-alias-placeholder INFO fires once per distinct stored value,
    not on every 30 s pass (the stored row repeats identically forever)."""
    import logging

    from paper_ingestion.main import _reconcile_litellm_models_once

    pool, conn = _make_pool()
    conn.fetchrow.side_effect = [
        {"value": "smart"},  # llm.smart_model — bare alias placeholder (pass 1)
        {"value": "qwen3:4b"},  # llm.fast_model
        None,  # llm.embed_model
        {"value": "smart"},  # pass 2 — same placeholder
        {"value": "qwen3:4b"},
        None,
    ]

    with caplog.at_level(logging.INFO, logger="paper_ingestion.main"):
        with contextlib.ExitStack() as stack:
            for p in _reconcile_patches(
                AsyncMock(return_value=True), AsyncMock(return_value=False)
            ):
                stack.enter_context(p)
            await _reconcile_litellm_models_once(pool)
            await _reconcile_litellm_models_once(pool)

    placeholder_logs = [r for r in caplog.records if "alias placeholder" in r.message]
    assert len(placeholder_logs) == 1


@pytest.mark.asyncio
async def test_reconcile_embed_mismatch_warned_once_per_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The legacy embed-row mismatch WARNING fires once per distinct value,
    not on every 30 s pass."""
    import logging

    from paper_ingestion.main import _reconcile_litellm_models_once

    pool, conn = _make_pool()
    conn.fetchrow.side_effect = [
        None,  # llm.smart_model (pass 1)
        None,  # llm.fast_model
        {"value": "mxbai-embed-large"},  # llm.embed_model — legacy mismatch
        None,  # pass 2
        None,
        {"value": "mxbai-embed-large"},
    ]

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.main"):
        with contextlib.ExitStack() as stack:
            for p in _reconcile_patches(
                AsyncMock(return_value=True), AsyncMock(return_value=False)
            ):
                stack.enter_context(p)
            await _reconcile_litellm_models_once(pool)
            await _reconcile_litellm_models_once(pool)

    embed_warnings = [r for r in caplog.records if "YAML-seeded" in r.message]
    assert len(embed_warnings) == 1


@pytest.mark.asyncio
async def test_reconciler_loop_is_persistent_across_success() -> None:
    """The loop keeps polling on the 30 s cadence even AFTER a successful pass.

    Persistence is the contract: a LiteLLM that later restarts DB-less comes
    back with NO smart/fast deployments (de-seeded YAML), so a stopped loop
    would leave the deployment LLM-dead until a service restart. Only
    cancellation (lifespan teardown) ends the loop.
    """
    from paper_ingestion.main import (
        _LITELLM_RECONCILE_INTERVAL_SECONDS,
        _litellm_model_reconciler_loop,
    )

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    mock_reconcile = AsyncMock(side_effect=[False, True, True])
    with (
        patch(
            "paper_ingestion.litellm_reconciler._reconcile_litellm_models_once",
            new=mock_reconcile,
        ),
        patch("paper_ingestion.litellm_reconciler.asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await _litellm_model_reconciler_loop(MagicMock())

    # Three passes ran (failure AND successes), with a sleep after each —
    # success does not break the loop.
    assert mock_reconcile.await_count == 3
    assert sleeps == [_LITELLM_RECONCILE_INTERVAL_SECONDS] * 3


@pytest.mark.asyncio
async def test_start_litellm_reconciler_can_be_disabled_for_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The maintenance flag skips only the background reconciler task."""
    from fastapi import FastAPI
    from paper_ingestion.main import (
        _shutdown_litellm_reconciler,
        _start_litellm_reconciler,
    )

    app = FastAPI()
    app.state.db_pool = MagicMock()
    monkeypatch.setenv("JARVIS_LITELLM_RECONCILER_ENABLED", "false")

    with caplog.at_level("INFO", logger="paper_ingestion.main"):
        await _start_litellm_reconciler(app)

    assert app.state.litellm_reconciler_task is None
    assert "LiteLLM model reconciler disabled" in caplog.text
    await _shutdown_litellm_reconciler(app)


@pytest.mark.asyncio
async def test_start_and_shutdown_reconciler_hooks_cancel_cleanly() -> None:
    """_start creates the named background task; _shutdown cancels and awaits it."""
    from fastapi import FastAPI
    from paper_ingestion.main import (
        _shutdown_litellm_reconciler,
        _start_litellm_reconciler,
    )

    started = asyncio.Event()

    async def _never_done(pool: Any) -> bool:
        started.set()
        await asyncio.Event().wait()  # blocks until cancelled
        return False

    app = FastAPI()
    app.state.db_pool = MagicMock()
    with patch(
        "paper_ingestion.litellm_reconciler._reconcile_litellm_models_once",
        new=_never_done,
    ):
        await _start_litellm_reconciler(app)
        task = app.state.litellm_reconciler_task
        assert isinstance(task, asyncio.Task)
        assert task.get_name() == "litellm_model_reconciler"
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert not task.done()

        await _shutdown_litellm_reconciler(app)
        assert task.done()
        assert task.cancelled()


# ---------------------------------------------------------------------------
# Registration guard: assert→RuntimeError (survives python -O)
# ---------------------------------------------------------------------------


def test_register_paper_ingestion_tasks_raises_when_kind_unregistered(monkeypatch):
    """A registration gap must raise a real (non-assert, -O-proof) exception."""
    import procrastinate
    import pytest
    from procrastinate.contrib.aiopg import AiopgConnector

    import paper_ingestion._task_register as reg

    app = procrastinate.App(connector=AiopgConnector())
    # Stub register_tasks so NO kinds get added → every kind is "missing".
    monkeypatch.setattr(reg, "register_tasks", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="failed to register kinds"):
        reg.register_paper_ingestion_tasks(app)


# ---------------------------------------------------------------------------
# C.1 post-pool runtime-config boot gate: wiring + fresh-DB skip
# ---------------------------------------------------------------------------


def test_lifespan_config_includes_runtime_validator_hook() -> None:
    """_validate_runtime_config_hook runs right after migrations, with a None teardown."""
    from paper_ingestion.main import (
        _lifespan_config,
        _run_migrations_hook,
        _validate_runtime_config_hook,
    )

    init = _lifespan_config.custom_init_tasks
    assert _validate_runtime_config_hook in init
    idx = init.index(_validate_runtime_config_hook)
    # Must run immediately after migrations so users/user_config exist.
    assert init[idx - 1] is _run_migrations_hook
    # Paired None teardown at the same index; the two lists stay equal-length.
    assert _lifespan_config.custom_teardown_tasks[idx] is None
    assert len(init) == len(_lifespan_config.custom_teardown_tasks)


@pytest.mark.asyncio
async def test_runtime_validator_hook_swallows_fresh_db_undefined_table() -> None:
    """A fresh pre-migration DB (UndefinedTableError) must not abort boot."""
    import asyncpg
    from fastapi import FastAPI

    from paper_ingestion.main import _validate_runtime_config_hook

    app = FastAPI()
    app.state.db_pool = MagicMock()

    with patch(
        "paper_ingestion.main.validate_runtime_config",
        new=AsyncMock(side_effect=asyncpg.UndefinedTableError('relation "users" does not exist')),
    ):
        # Must NOT raise — the hook swallows the fresh-DB error and boot proceeds.
        await _validate_runtime_config_hook(app)
