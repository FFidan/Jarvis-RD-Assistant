"""Tests for the shared jobs-router factory.

Covers genuinely-shared behavior (not duplicated by per-service tests):

* permissive mode rejects unknown kinds with HTTP 400 (LE-002).
* discriminated mode validates per-kind payload schemas at parse time.
* ownership / serialization helpers behave consistently for both services.
* SYM-002: ``CreateJobRequest.payload`` uses a non-shared default.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from jarvis_common import jobs_router as jobs_router_mod
from jarvis_common.jobs_router import build_jobs_router, serialise_row
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


class _FakeLimiter:
    """Decorator-only stub of slowapi.Limiter — no-op limit() that preserves func."""

    enabled = False

    def limit(self, _spec: str):
        def _decorator(func):
            return func

        return _decorator


def _build_factory(*, schemas=None, kinds=frozenset({"foo.bar"})):
    """Build a router with stub deps and return (router, request_model, dep_pool)."""
    pool_marker = MagicMock(name="db_pool")

    def _get_pool() -> Any:
        return pool_marker

    router = build_jobs_router(
        service_name="test_service",
        public_kinds=kinds,
        get_db_pool=_get_pool,
        limiter=_FakeLimiter(),
        payload_schemas=schemas,
    )
    request_model = router.create_job_request_model  # type: ignore[attr-defined]
    handlers = {r.endpoint.__name__: r.endpoint for r in router.routes}
    return router, request_model, pool_marker, handlers


# ---------------------------------------------------------------------------
# DRY-001: factory rejects unknown kind with 400 in permissive mode (LE-002)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_jobs_router_unknown_kind_returns_400():
    """LE-002 contract: permissive mode → unknown kinds raise HTTP 400."""
    _router, request_model, pool, handlers = _build_factory(
        schemas=None, kinds=frozenset({"card.generate"})
    )

    with pytest.raises(HTTPException) as exc:
        await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="totally.unknown"),
            db_pool=pool,
            user_id=None,
        )

    assert exc.value.status_code == 400
    assert "totally.unknown" in exc.value.detail


@pytest.mark.asyncio
async def test_build_jobs_router_known_kind_enqueues():
    """Permissive mode: kind in allowlist → dispatches to KIND_TO_TASK and returns job_id.

    After the B.4 Step 3 cutover, ``create_job`` dispatches via
    ``KIND_TO_TASK.defer_async`` for all 19 registered kinds (including
    ``card.generate``).  The test patches ``defer_async`` on the task object
    to avoid a live procrastinate connection.
    """
    _router, request_model, pool, handlers = _build_factory(
        schemas=None, kinds=frozenset({"card.generate"})
    )

    fake_task = AsyncMock()
    fake_task.defer_async = AsyncMock(return_value=None)
    fake_kind_to_task = {"card.generate": fake_task}

    with patch.dict("jarvis_common.task_registry._TASK_MAP", fake_kind_to_task, clear=True):
        result = await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="card.generate", payload={"paper_id": 1}),
            db_pool=pool,
            user_id=42,
        )

    assert result.status == "queued"
    assert isinstance(result.job_id, str)
    # defer_async must be called with the reserved keys + payload spread.
    call_kwargs = fake_task.defer_async.await_args.kwargs
    assert call_kwargs["user_id"] == 42
    assert call_kwargs["paper_id"] == 1
    assert "job_id" in call_kwargs


# ---------------------------------------------------------------------------
# Discriminated mode — payload schemas reject mis-shaped or unknown kinds
# ---------------------------------------------------------------------------


class _ProcessPayload(BaseModel):
    kind: Literal["paper.process"]
    paper_id: int


class _AnalyzePayload(BaseModel):
    kind: Literal["paper.analyze"]
    paper_id: int


def test_build_jobs_router_validates_payload_against_schema_when_provided():
    """Discriminated mode: missing required field raises Pydantic ValidationError."""
    _router, request_model, _pool, _handlers = _build_factory(
        schemas={"paper.process": _ProcessPayload, "paper.analyze": _AnalyzePayload},
        kinds=frozenset({"paper.process", "paper.analyze"}),
    )

    # Missing paper_id → discriminated union rejects at parse time (HTTP 422 in API).
    with pytest.raises(ValidationError):
        request_model(kind="paper.process", payload={})

    # Wrong-typed paper_id → also rejected.
    with pytest.raises(ValidationError):
        request_model(kind="paper.process", payload={"paper_id": "not-an-int"})

    # Unknown kind → discriminator rejects.
    with pytest.raises(ValidationError):
        request_model(kind="bogus.kind", payload={})

    # Valid payload → accepted.
    req = request_model(kind="paper.process", payload={"paper_id": 42})
    assert req.kind == "paper.process"
    assert req.payload["paper_id"] == 42


# ---------------------------------------------------------------------------
# serialise_row — consistent JSON-safe row formatting
# ---------------------------------------------------------------------------


def test_build_jobs_router_serializes_row_consistently():
    """serialise_row converts UUIDs / datetimes to strings; passes through scalars."""
    job_id = uuid.UUID("00000000-0000-0000-0000-000000000007")
    created = _dt.datetime(2026, 4, 26, 12, 0, 0, tzinfo=_dt.UTC)

    row = {
        "id": job_id,
        "kind": "card.generate",
        "status": "queued",
        "created_at": created,
        "user_id": "1",
        "payload": {"paper_id": 1},
        "progress": 0.5,
        "error": None,
    }
    out = serialise_row(row)

    # UUID has isoformat? No — but datetime does. UUIDs are returned as-is unless
    # they expose isoformat. Confirm both pass-through and conversion happen.
    assert out["created_at"] == "2026-04-26T12:00:00+00:00"
    # Non-isoformat values pass through unchanged.
    assert out["status"] == "queued"
    assert out["payload"] == {"paper_id": 1}
    assert out["progress"] == 0.5
    assert out["error"] is None


# ---------------------------------------------------------------------------
# SYM-002: payload default is not a shared mutable
# ---------------------------------------------------------------------------


def test_create_job_request_default_payload_not_shared_permissive_mode():
    _router, request_model, _pool, _handlers = _build_factory(
        schemas=None, kinds=frozenset({"card.generate"})
    )
    a = request_model(kind="card.generate")
    b = request_model(kind="card.generate")
    a.payload["mark"] = True
    assert "mark" not in b.payload, "SYM-002 regression: payload default is shared"


def test_create_job_request_default_payload_not_shared_discriminated_mode():
    _router, request_model, _pool, _handlers = _build_factory(
        schemas={"paper.analyze": _AnalyzePayload},
        kinds=frozenset({"paper.analyze"}),
    )
    a = request_model(kind="paper.analyze", payload={"paper_id": 1})
    b = request_model(kind="paper.analyze", payload={"paper_id": 2})
    a.payload["mark"] = True
    assert "mark" not in b.payload


# ---------------------------------------------------------------------------
# Ownership coercion (LE-002 — Sprint 4 fix preserved by factory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_owner_coerces_str_int():
    """Row user_id='3' must match int caller user_id=3 (str-coerced compare)."""
    _router, _request_model, pool, handlers = _build_factory()
    row = {"id": "j1", "user_id": "3", "kind": "foo.bar", "status": "queued"}

    with patch.object(jobs_router_mod.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        result = await handlers["get_job"](
            request=MagicMock(),
            job_id="j1",
            db_pool=pool,
            user_id=3,
        )
    assert result["id"] == "j1"


@pytest.mark.asyncio
async def test_get_job_owner_mismatch_returns_404():
    _router, _request_model, pool, handlers = _build_factory()
    row = {"id": "j2", "user_id": "3", "kind": "foo.bar", "status": "queued"}

    with patch.object(jobs_router_mod.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc:
            await handlers["get_job"](
                request=MagicMock(),
                job_id="j2",
                db_pool=pool,
                user_id=99,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_job_null_owner_is_public():
    """Row.user_id=None → publicly accessible (single-tenant fallback)."""
    _router, _request_model, pool, handlers = _build_factory()
    row = {"id": "j3", "user_id": None, "kind": "foo.bar", "status": "queued"}

    with patch.object(jobs_router_mod.jobs_lib, "get_unified", AsyncMock(return_value=row)):
        result = await handlers["get_job"](
            request=MagicMock(),
            job_id="j3",
            db_pool=pool,
            user_id=None,
        )
    assert result["id"] == "j3"


# ---------------------------------------------------------------------------
# Discriminated mode + runtime allowlist guard returns 422 (PI semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discriminated_runtime_allowlist_returns_422(monkeypatch):
    """Kind that survives parsing but is not in the runtime allowlist → 422.

    This guards the discriminated-mode contract: a kind that is shape-valid
    (parsing succeeds via the union) but absent from the runtime allowlist
    must be rejected with 422 to mirror parse-time semantics.
    """
    # Force JARVIS_ENABLE_TEST_JOBS off so noop.test is not auto-added to the
    # allowlist by _public_kinds_now() (test_jobs.py sets it module-level).
    monkeypatch.delenv("JARVIS_ENABLE_TEST_JOBS", raising=False)

    class _UnusedPayload(BaseModel):
        kind: Literal["unused.kind"]
        model_config = {"extra": "allow"}

    _router, request_model, pool, handlers = _build_factory(
        schemas={"paper.process": _ProcessPayload, "unused.kind": _UnusedPayload},
        kinds=frozenset({"paper.process"}),  # unused.kind deliberately excluded
    )

    # Parsing succeeds (unused.kind is in the union).
    body = request_model(kind="unused.kind", payload={})

    with pytest.raises(HTTPException) as exc:
        await handlers["create_job"](
            request=MagicMock(),
            body=body,
            db_pool=pool,
            user_id=None,
        )
    # Discriminated mode preserves PI's 422 semantics for allowlist mismatches.
    assert exc.value.status_code == 422
    assert "unused.kind" in exc.value.detail


# ---------------------------------------------------------------------------
# WS-6B-α — paper_ownership_extractor hook wiring
# ---------------------------------------------------------------------------


def _build_factory_with_owner_hook(*, kinds=frozenset({"foo.bar"})):
    """Build a router that always extracts ``payload['paper_id']`` for ownership."""
    pool_marker = MagicMock(name="db_pool")
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool_marker.acquire.return_value = ctx

    def _get_pool() -> Any:
        return pool_marker

    router = build_jobs_router(
        service_name="test_service",
        public_kinds=kinds,
        get_db_pool=_get_pool,
        limiter=_FakeLimiter(),
        payload_schemas=None,
        paper_ownership_extractor=lambda p: (
            p.get("paper_id") if isinstance(p.get("paper_id"), int) else None
        ),
    )
    request_model = router.create_job_request_model  # type: ignore[attr-defined]
    handlers = {r.endpoint.__name__: r.endpoint for r in router.routes}
    return router, request_model, pool_marker, conn, handlers


@pytest.mark.asyncio
async def test_create_job_403_when_paper_owned_by_other_user():
    """Sprint B: caller=99, paper.discovered_by=42 + caller not in library → 403."""
    _r, request_model, pool, conn, handlers = _build_factory_with_owner_hook()
    # Sprint B canonical-corpus: the ownership probe reads ``discovered_by``
    # then falls back to a ``user_library`` membership check. Return a row
    # discovered by user 42 and force the library fetchval probe to MISS.
    conn.fetchrow.return_value = {"discovered_by": 42}
    conn.fetchval.return_value = None  # not in caller's library

    fake_task = AsyncMock()
    fake_task.defer_async = AsyncMock(return_value=None)
    with (
        patch.dict("jarvis_common.task_registry._TASK_MAP", {"foo.bar": fake_task}, clear=True),
        pytest.raises(HTTPException) as exc,
    ):
        await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="foo.bar", payload={"paper_id": 7}),
            db_pool=pool,
            user_id=99,
        )
    assert exc.value.status_code == 403
    fake_task.defer_async.assert_not_called()  # ownership failure must abort before defer


# ---------------------------------------------------------------------------
# B.4 Step 2 — SSE stream surfaces both legacy and procrastinate sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_job_route_returns_legacy_sse_payload():
    """B.4 SSE bridge: GET /api/jobs/{id}/stream still emits legacy SSE frames.

    This guards against a regression where adding the procrastinate listen path
    silently broke the existing legacy SSE behavior. The endpoint must:
      1. 404 on owner mismatch (unchanged — see WS-6B-α tests above)
      2. emit a legacy ``data:`` frame whose JSON has ``source='legacy'``
    """
    import json
    from collections.abc import AsyncIterator

    _router, _rm, pool, handlers = _build_factory()

    legacy_row = {
        "id": "uuid-stream-1",
        "kind": "foo.bar",
        "status": "succeeded",
        "progress": 1.0,
        "progress_message": "done",
        "payload": {"x": 1},
        "result": {"ok": True},
        "error": None,
        "user_id": None,
    }

    async def _fake_stream(_pool, _job_id, *, is_disconnected) -> AsyncIterator[str]:
        # Mirror the real shape — a single legacy frame then EOF.
        yield (
            "data: "
            + json.dumps(
                {
                    "progress": 1.0,
                    "progress_message": "done",
                    "status": "succeeded",
                    "source": "legacy",
                    "result": {"ok": True},
                    "payload": {"x": 1},
                }
            )
            + "\n\n"
        )

    with (
        patch.object(jobs_router_mod.jobs_lib, "get_unified", AsyncMock(return_value=legacy_row)),
        patch.object(jobs_router_mod.jobs_lib, "stream_job_events", _fake_stream),
    ):
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        response = await handlers["stream_job"](
            request=request,
            job_id="uuid-stream-1",
            db_pool=pool,
            user_id=None,
        )

    # Drain the StreamingResponse body iterator.
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    assert chunks, "stream must emit at least one frame"
    body = "".join(chunks)
    assert "data:" in body
    payload_line = body.strip().splitlines()[0].removeprefix("data: ")
    parsed = json.loads(payload_line)
    assert parsed["source"] == "legacy"
    assert parsed["status"] == "succeeded"


@pytest.mark.asyncio
async def test_stream_job_route_surfaces_procrastinate_source():
    """B.4 SSE bridge (router-level): a procrastinate-sourced frame propagates through StreamingResponse."""
    import json
    from collections.abc import AsyncIterator

    _router, _rm, pool, handlers = _build_factory()

    legacy_row = {
        "id": "uuid-stream-2",
        "kind": "foo.bar",
        "status": "queued",
        "progress": None,
        "progress_message": None,
        "payload": None,
        "result": None,
        "error": None,
        "user_id": None,
    }

    async def _fake_stream(_pool, _job_id, *, is_disconnected) -> AsyncIterator[str]:
        # Emit a procrastinate-tagged frame for a job not yet in the legacy table.
        yield (
            "data: "
            + json.dumps(
                {
                    "progress": None,
                    "progress_message": None,
                    "status": "succeeded",
                    "source": "procrastinate",
                    "payload": {"job_id": "uuid-stream-2", "paper_id": 7},
                }
            )
            + "\n\n"
        )

    with (
        patch.object(jobs_router_mod.jobs_lib, "get_unified", AsyncMock(return_value=legacy_row)),
        patch.object(jobs_router_mod.jobs_lib, "stream_job_events", _fake_stream),
    ):
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        response = await handlers["stream_job"](
            request=request,
            job_id="uuid-stream-2",
            db_pool=pool,
            user_id=None,
        )

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    body = "".join(chunks)
    assert '"source": "procrastinate"' in body
    payload_line = body.strip().splitlines()[0].removeprefix("data: ")
    parsed = json.loads(payload_line)
    assert parsed["source"] == "procrastinate"
    assert parsed["status"] == "succeeded"
    assert parsed["payload"]["paper_id"] == 7


# ---------------------------------------------------------------------------
# B.4 Step 3 — procrastinate-only route parity (Bug 2 regression tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_job_route_finds_procrastinate_only_job():
    """Bug 2 regression: stream_job must NOT 404 for procrastinate-only jobs.

    Previously the route called ``jobs_lib.get`` which queries only the legacy
    table.  Procrastinate-only rows (no legacy row) returned 404.  After the
    fix, ``get_unified`` falls through to the procrastinate table.

    Setup: ``jobs_lib.get`` returns None (no legacy row) and
    ``get_procrastinate_job_for_jarvis_id`` returns a procrastinate prow.
    """
    import json
    from collections.abc import AsyncIterator

    _router, _rm, pool, handlers = _build_factory()

    procrastinate_row = {
        "id": "p-stream-only",
        "kind": "digest.weekly",
        "status": "queued",
        "progress": 0,
        "progress_message": None,
        "payload": {},
        "result": None,
        "error": None,
        "user_id": None,
        "cancel_requested": False,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "source": "procrastinate",
    }

    async def _fake_stream(_pool, _job_id, *, is_disconnected) -> AsyncIterator[str]:
        yield (
            "data: "
            + json.dumps({"status": "queued", "source": "procrastinate", "progress": None})
            + "\n\n"
        )

    with (
        # get_unified: legacy returns None, procrastinate returns the row
        patch.object(
            jobs_router_mod.jobs_lib, "get_unified", AsyncMock(return_value=procrastinate_row)
        ),
        patch.object(jobs_router_mod.jobs_lib, "stream_job_events", _fake_stream),
    ):
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        # Must NOT raise HTTPException(404).
        response = await handlers["stream_job"](
            request=request,
            job_id="p-stream-only",
            db_pool=pool,
            user_id=None,
        )

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    body = "".join(chunks)
    assert "data:" in body, "stream must emit at least one SSE frame"
    parsed = json.loads(body.strip().splitlines()[0].removeprefix("data: "))
    assert parsed["source"] == "procrastinate"


@pytest.mark.asyncio
async def test_get_job_route_finds_procrastinate_only():
    """Bug 2 parity: GET /api/jobs/{id} must return 200 for procrastinate-only jobs.

    ``get_unified`` falls through to procrastinate; the route returns the row.
    """
    _router, _rm, pool, handlers = _build_factory()

    procrastinate_row = {
        "id": "p-get-only",
        "kind": "paper.process",
        "status": "running",
        "progress": 0,
        "progress_message": None,
        "payload": {"paper_id": 7},
        "result": None,
        "error": None,
        "user_id": None,
        "cancel_requested": False,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "source": "procrastinate",
    }

    with patch.object(
        jobs_router_mod.jobs_lib, "get_unified", AsyncMock(return_value=procrastinate_row)
    ):
        result = await handlers["get_job"](
            request=MagicMock(),
            job_id="p-get-only",
            db_pool=pool,
            user_id=None,
        )

    assert result["id"] == "p-get-only"
    assert result["status"] == "running"
    assert result["source"] == "procrastinate"


@pytest.mark.asyncio
async def test_cancel_job_route_calls_procrastinate_cancel():
    """cancel_job must dispatch to procrastinate.cancel_job_by_id_async for procrastinate rows."""
    _router, _rm, pool, handlers = _build_factory()

    procrastinate_unified_row = {
        "id": "p-cancel-only",
        "kind": "paper.process",
        "status": "running",
        "user_id": None,
        "source": "procrastinate",
    }
    procrastinate_prow = {
        "id": 99,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": "p-cancel-only", "paper_id": 7},
        "attempts": 1,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }

    fake_job_manager = AsyncMock()
    fake_job_manager.cancel_job_by_id_async = AsyncMock(return_value=None)
    fake_app = MagicMock()
    fake_app.job_manager = fake_job_manager

    with (
        patch.object(
            jobs_router_mod.jobs_lib,
            "get_unified",
            AsyncMock(return_value=procrastinate_unified_row),
        ),
        patch.object(
            jobs_router_mod.jobs_lib,
            "get_procrastinate_job_for_jarvis_id",
            AsyncMock(return_value=procrastinate_prow),
        ),
        patch("jarvis_common.task_registry.app", fake_app),
    ):
        result = await handlers["cancel_job"](
            request=MagicMock(),
            job_id="p-cancel-only",
            db_pool=pool,
            user_id=None,
        )

    assert result == {"ok": True}
    fake_job_manager.cancel_job_by_id_async.assert_awaited_once_with(99, abort=True)


@pytest.mark.asyncio
async def test_create_job_route_dispatches_to_task_registry():
    """create_job must dispatch via KIND_TO_TASK.defer_async for registered kinds."""
    _router, request_model, pool, handlers = _build_factory(kinds=frozenset({"paper.process"}))

    fake_task = AsyncMock()
    fake_task.defer_async = AsyncMock(return_value=None)
    fake_kind_to_task = {"paper.process": fake_task}

    with patch.dict("jarvis_common.task_registry._TASK_MAP", fake_kind_to_task, clear=True):
        result = await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="paper.process", payload={"paper_id": 42}),
            db_pool=pool,
            user_id=7,
        )

    assert result.status == "queued"
    assert isinstance(result.job_id, str)
    # defer_async must have been called with the reserved keys + payload.
    call_kwargs = fake_task.defer_async.await_args.kwargs
    assert "job_id" in call_kwargs
    assert call_kwargs["user_id"] == 7
    assert call_kwargs["paper_id"] == 42


# ---------------------------------------------------------------------------
# W3-DRY-5 — noop.test is a valid job kind when JARVIS_ENABLE_TEST_JOBS=1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_noop_test_returns_201(monkeypatch):
    """W3-DRY-5: kind='noop.test' is accepted and dispatched when JARVIS_ENABLE_TEST_JOBS=1.

    _public_kinds_now() adds 'noop.test' to the allowlist when the env toggle
    is on.  KIND_TO_TASK must also map 'noop.test' to a callable task so that
    create_job does not raise HTTP 400 after the allowlist check passes.
    """
    monkeypatch.setenv("JARVIS_ENABLE_TEST_JOBS", "1")

    # Build the router with JARVIS_ENABLE_TEST_JOBS already set so that
    # _public_kinds_now() includes 'noop.test' at request time.
    _router, request_model, pool, handlers = _build_factory(kinds=frozenset())

    fake_task = AsyncMock()
    fake_task.defer_async = AsyncMock(return_value=None)
    fake_kind_to_task = {"noop.test": fake_task}

    with patch.dict("jarvis_common.task_registry._TASK_MAP", fake_kind_to_task, clear=True):
        result = await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="noop.test"),
            db_pool=pool,
            user_id=None,
        )

    assert result.status == "queued"
    assert isinstance(result.job_id, str)
    # defer_async must be called with the reserved keys.
    call_kwargs = fake_task.defer_async.await_args.kwargs
    assert "job_id" in call_kwargs
    assert call_kwargs["user_id"] is None


@pytest.mark.asyncio
async def test_create_job_noop_test_rejected_when_flag_off(monkeypatch):
    """W3-DRY-5 inverse: kind='noop.test' is rejected when JARVIS_ENABLE_TEST_JOBS is unset."""
    monkeypatch.delenv("JARVIS_ENABLE_TEST_JOBS", raising=False)

    _router, request_model, pool, handlers = _build_factory(kinds=frozenset())

    with pytest.raises(HTTPException) as exc:
        await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="noop.test"),
            db_pool=pool,
            user_id=None,
        )

    assert exc.value.status_code == 400
    assert "noop.test" in exc.value.detail
