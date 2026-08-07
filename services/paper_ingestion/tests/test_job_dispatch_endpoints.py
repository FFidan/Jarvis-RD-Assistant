"""Job endpoint contract tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.advisory_lock import _kind_lock_key
from jarvis_common.testing import make_pool_and_conn


@pytest.fixture(autouse=True)
def _dev_mode_for_validation_assertions(monkeypatch):
    """Production mode redacts pydantic loc/errors; tests in this
    file assert on those details, so force DEV_MODE=true."""
    monkeypatch.setenv("DEV_MODE", "true")


@pytest.fixture()
def app_with_pool():
    """Create the paper_ingestion app with DB/auth dependencies overridden.

    RD-DA-002: create_job now uses current_user_id_strict (was nullable).
    Override it alongside verify_api_key so validation tests reach the
    discriminator/allowlist logic instead of getting a 401 first.
    """
    from jarvis_common.auth import (
        current_user_id_strict,
        get_current_user_id_or_bot,
        require_admin,
        require_admin_or_api_key,
        verify_api_key,
    )
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    pool, _conn = make_pool_and_conn()
    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            # The pulse_app factory below adds per-test overrides for these
            # two seams; declaring them here removes any in-test write again
            # on exit so it cannot leak past the fixture.
            dependency_absent=(get_current_user_id_or_bot, require_admin_or_api_key),
            dependency_overrides={
                verify_api_key: lambda: None,
                current_user_id_strict: lambda: 42,
                require_admin: lambda: None,
            },
        ),
    ):
        yield app, pool


async def test_summarize_endpoint_enqueues_job(app_with_pool):
    """POST /api/summarize/{paper_id} returns a durable job id."""
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    app, _pool = app_with_pool
    mock_task = MagicMock()
    defer_async = AsyncMock()
    mock_task.defer_async = defer_async
    with patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/summarize/42")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]  # UUID assigned at runtime
    defer_async.assert_awaited_once()
    call_kwargs = defer_async.call_args.kwargs
    assert call_kwargs["paper_id"] == 42
    assert "job_id" in call_kwargs
    assert "user_id" in call_kwargs
    assert call_kwargs["force"] is False  # absent body defaults to non-forced


async def test_summarize_endpoint_forwards_force_flag(app_with_pool):
    """POST /api/summarize/{paper_id} with {"force": true} threads force into the job payload."""
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    app, _pool = app_with_pool
    mock_task = MagicMock()
    defer_async = AsyncMock()
    mock_task.defer_async = defer_async
    with patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/summarize/42", json={"force": True})

    assert resp.status_code == 202
    defer_async.assert_awaited_once()
    assert defer_async.call_args.kwargs["force"] is True


async def test_extract_endpoint_enqueues_single_extraction_job(app_with_pool):
    """POST /api/papers/{paper_id}/extract returns a durable job id."""
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    app, _pool = app_with_pool
    mock_task = MagicMock()
    defer_async = AsyncMock()
    mock_task.defer_async = defer_async
    with patch.dict(task_registry._TASK_MAP, {"extraction.single": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/papers/42/extract", json={"template_id": 3})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]  # UUID assigned at runtime
    defer_async.assert_awaited_once()
    call_kwargs = defer_async.call_args.kwargs
    assert call_kwargs["paper_id"] == 42
    assert call_kwargs["template_id"] == 3
    assert "job_id" in call_kwargs
    assert "user_id" in call_kwargs


async def test_scan_local_pdfs_endpoint_enqueues_job(app_with_pool):
    """POST /api/scan-local-pdfs returns a durable job id instead of blocking."""
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    app, _pool = app_with_pool
    mock_task = MagicMock()
    defer_async = AsyncMock()
    mock_task.defer_async = defer_async
    with patch.dict(task_registry._TASK_MAP, {"papers.scan_local": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/scan-local-pdfs")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]  # UUID assigned at runtime
    defer_async.assert_awaited_once()
    call_kwargs = defer_async.call_args.kwargs
    assert "job_id" in call_kwargs
    assert "user_id" in call_kwargs


async def test_scan_local_pdfs_non_admin_gets_403(app_with_pool):
    """POST /api/scan-local-pdfs returns 403 for non-admin callers."""
    from fastapi import HTTPException
    from jarvis_common.auth import require_admin

    app, _pool = app_with_pool

    def _deny_admin():
        raise HTTPException(status_code=403, detail="Admin required")

    app.dependency_overrides[require_admin] = _deny_admin
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/scan-local-pdfs")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Discriminated-union payload validation tests
# ---------------------------------------------------------------------------


async def test_create_job_rejects_unknown_kind(app_with_pool):
    """POST /api/jobs with an unknown kind returns 422."""
    app, _pool = app_with_pool
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/jobs",
            json={"kind": "bogus.kind", "payload": {}},
        )

    assert resp.status_code == 422
    body = resp.json()
    # The service's validation-error handler puts structured errors in ``errors``.
    errors = body.get("errors") or body.get("detail") or []
    errors_str = str(errors)
    # Discriminator error surfaces the invalid tag in the message.
    assert (
        "bogus.kind" in errors_str
        or "union_tag_invalid" in errors_str
        or "discriminator" in errors_str
    )


async def test_create_job_rejects_missing_paper_id_for_paper_process(app_with_pool):
    """POST /api/jobs kind=paper.process with empty payload returns 422."""
    app, _pool = app_with_pool
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/jobs",
            json={"kind": "paper.process", "payload": {}},
        )

    assert resp.status_code == 422
    body = resp.json()
    errors_str = str(body.get("errors") or body.get("detail") or [])
    assert "paper_id" in errors_str


async def test_create_job_rejects_string_paper_id(app_with_pool):
    """POST /api/jobs kind=paper.process with paper_id as string returns 422."""
    app, _pool = app_with_pool
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/jobs",
            json={"kind": "paper.process", "payload": {"paper_id": "not-an-int"}},
        )

    assert resp.status_code == 422
    body = resp.json()
    errors_str = str(body.get("errors") or body.get("detail") or [])
    # Pydantic reports the field that failed int coercion.
    assert "paper_id" in errors_str or "int_parsing" in errors_str


# ---------------------------------------------------------------------------
# Batch payload size bound
# ---------------------------------------------------------------------------

_BATCH_KINDS = ("papers.batch_process", "papers.batch_summarize", "extraction.batch")


@pytest.mark.parametrize("kind", _BATCH_KINDS)
async def test_batch_payload_rejects_more_than_fifty_paper_ids(app_with_pool, kind):
    """One request may not enqueue an unbounded amount of per-paper work."""
    app, _pool = app_with_pool
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/jobs",
            json={"kind": kind, "payload": {"paper_ids": list(range(1, 52))}},
        )

    assert resp.status_code == 422
    errors_str = str(resp.json().get("errors") or resp.json().get("detail") or [])
    assert "paper_ids" in errors_str


@pytest.mark.parametrize("kind", _BATCH_KINDS)
async def test_batch_payload_accepts_fifty_paper_ids(app_with_pool, kind):
    """The bound is 50, not 49 — the largest legitimate batch still enqueues."""
    import jarvis_common.task_registry as task_registry
    from paper_ingestion.deps import get_db_pool

    app, _pool = app_with_pool
    paper_ids = list(range(1, 51))
    pool, conn = make_pool_and_conn()
    conn.fetch = AsyncMock(return_value=[{"id": pid, "is_visible": True} for pid in paper_ids])
    app.dependency_overrides[get_db_pool] = lambda: pool

    mock_task = MagicMock(defer_async=AsyncMock())
    with patch.dict(task_registry._TASK_MAP, {kind: mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/jobs",
                json={"kind": kind, "payload": {"paper_ids": paper_ids}},
            )

    assert resp.status_code == 202, resp.text
    mock_task.defer_async.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /api/pulse/generate — duplicate-run gate and duplicate-enqueue gate
# ---------------------------------------------------------------------------

_RUN_LOCK = _kind_lock_key("pulse.generate")
_ENQUEUE_LOCK = _kind_lock_key("pulse.generate.enqueue")
_PULSE_UID = 42


def _routed(sql: str, expected: str) -> None:
    """Refuse a statement this fake connection does not model."""
    if expected not in sql:
        raise NotImplementedError(f"the advisory-lock stub cannot answer: {sql}")


class _LockConn:
    """One pooled session of :class:`_AdvisoryLockPool`."""

    def __init__(self, pool: "_AdvisoryLockPool") -> None:
        self._pool = pool
        self.held: set[tuple[int, int]] = set()

    async def _try_lock(self, key: tuple[int, int]) -> bool:
        await asyncio.sleep(0)  # yield so a concurrent request can interleave here
        if key in self.held:
            return True  # advisory locks are re-entrant within one session
        if key in self._pool.owners:
            return False
        self._pool.owners[key] = self
        self.held.add(key)
        return True

    def _unlock(self, key: tuple[int, int]) -> None:
        if key in self.held:
            self.held.discard(key)
            self._pool.owners.pop(key, None)

    # The three methods below route by statement kind and refuse anything this
    # fake does not model, so a statement it cannot answer fails loudly instead
    # of receiving a stand-in reply. They describe what the stub supports, not
    # what the router is expected to emit.
    async def fetchval(self, sql: str, *args):
        _routed(sql, "pg_try_advisory_lock")
        return await self._try_lock((args[0], args[1]))

    async def fetchrow(self, sql: str, *args):
        if "pg_try_advisory_lock" in sql:
            return {"got": await self._try_lock((args[0], args[1]))}
        _routed(sql, "procrastinate_jobs")
        return None if self._pool.in_flight_id is None else {"id": self._pool.in_flight_id}

    async def execute(self, sql: str, *args) -> None:
        _routed(sql, "pg_advisory_unlock")
        self._unlock((args[0], args[1]))

    def release(self) -> None:
        for key in list(self.held):
            self._unlock(key)


class _Acquire:
    def __init__(self, pool: "_AdvisoryLockPool") -> None:
        self._pool = pool
        self._conn: _LockConn | None = None

    async def __aenter__(self) -> _LockConn:
        self._conn = _LockConn(self._pool)
        return self._conn

    async def __aexit__(self, *_exc) -> bool:
        assert self._conn is not None
        self._conn.release()
        return False


class _AdvisoryLockPool:
    """asyncpg-pool stub modelling session advisory locks by ``(key1, key2)``.

    Faithful enough to tell the two pulse lock keys apart, which is what the
    duplicate-run and duplicate-enqueue gates are built on.
    """

    def __init__(self, *, held_elsewhere=(), in_flight_id: int | None = None) -> None:
        self.owners: dict[tuple[int, int], object] = {key: object() for key in held_elsewhere}
        self.in_flight_id = in_flight_id
        self.locks_held_at_defer: list[set[tuple[int, int]]] = []

    def acquire(self) -> _Acquire:
        return _Acquire(self)


@pytest.fixture()
def pulse_app(app_with_pool):
    """The pulse app wired to a lock-modelling pool, with audit writes stubbed."""
    from jarvis_common.auth import get_current_user_id_or_bot, require_admin_or_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers import pulse as pulse_router

    app, _pool = app_with_pool

    def _build(*, held_elsewhere=(), in_flight_id=None):
        import jarvis_common.task_registry as task_registry

        pool = _AdvisoryLockPool(held_elsewhere=held_elsewhere, in_flight_id=in_flight_id)
        app.dependency_overrides[get_db_pool] = lambda: pool
        app.dependency_overrides[get_current_user_id_or_bot] = lambda: _PULSE_UID
        app.dependency_overrides[require_admin_or_api_key] = lambda: None

        async def _defer(**_kwargs):
            pool.locks_held_at_defer.append(set(pool.owners))
            # defer_async is an await: a concurrent request gets to run its own
            # checks here, before this job exists in the queue.
            await asyncio.sleep(0)
            pool.in_flight_id = 77  # the job this call just queued

        task = MagicMock(defer_async=AsyncMock(side_effect=_defer))
        patches = [
            patch.dict(task_registry._TASK_MAP, {"pulse.generate": task}),
            patch.object(pulse_router, "log_audit", AsyncMock()),
        ]
        for p in patches:
            p.start()
        _build.stack.extend(patches)
        return pool, task

    _build.stack = []
    yield _build
    for p in _build.stack:
        p.stop()


async def _post_generate(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/api/pulse/generate")


async def test_pulse_generate_409s_while_a_run_holds_the_lock(app_with_pool, pulse_app):
    """The shipped duplicate-RUN gate: a held ``pulse.generate`` lock means 409."""
    app, _ = app_with_pool
    _pool, task = pulse_app(held_elsewhere=[(_RUN_LOCK, _PULSE_UID)])

    resp = await _post_generate(app)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_running"
    assert detail["in_flight_job_id"] is None
    task.defer_async.assert_not_awaited()


async def test_pulse_generate_409_reports_the_callers_own_in_flight_job(app_with_pool, pulse_app):
    """The 409 body still carries the caller's own job id when one exists."""
    app, _ = app_with_pool
    pulse_app(held_elsewhere=[(_RUN_LOCK, _PULSE_UID)], in_flight_id=55)

    resp = await _post_generate(app)

    assert resp.status_code == 409
    assert resp.json()["detail"]["in_flight_job_id"] == 55


async def test_two_sequential_enqueues_produce_one_job(app_with_pool, pulse_app):
    """The run lock is free between the two calls, but the queued job is a duplicate."""
    app, _ = app_with_pool
    _pool, task = pulse_app()

    first = await _post_generate(app)
    second = await _post_generate(app)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["reason"] == "already_running"
    task.defer_async.assert_awaited_once()


async def test_two_concurrent_enqueues_produce_one_job(app_with_pool, pulse_app):
    """Both requests race through the check-then-act window; only one job is deferred."""
    app, _ = app_with_pool
    _pool, task = pulse_app()

    responses = await asyncio.gather(_post_generate(app), _post_generate(app))

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert task.defer_async.await_count == 1


async def test_enqueue_gate_leaves_the_run_lock_free_for_the_worker(app_with_pool, pulse_app):
    """Holding the run lock across the defer would self-block the in-process worker."""
    app, _ = app_with_pool
    pool, _task = pulse_app()

    resp = await _post_generate(app)

    assert resp.status_code == 200, resp.text
    assert pool.locks_held_at_defer == [{(_ENQUEUE_LOCK, _PULSE_UID)}], (
        "the deferred job must be able to take pulse.generate immediately"
    )
