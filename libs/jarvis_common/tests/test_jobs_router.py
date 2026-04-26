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
    """Permissive mode: kind in allowlist → enqueue called and job_id returned."""
    _router, request_model, pool, handlers = _build_factory(
        schemas=None, kinds=frozenset({"card.generate"})
    )

    with patch.object(jobs_router_mod.jobs_lib, "enqueue", AsyncMock(return_value="job-7")):
        result = await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="card.generate", payload={"paper_id": 1}),
            db_pool=pool,
            user_id=42,
        )

    assert result.job_id == "job-7"
    assert result.status == "queued"


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

    with patch.object(jobs_router_mod.jobs_lib, "get", AsyncMock(return_value=row)):
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

    with patch.object(jobs_router_mod.jobs_lib, "get", AsyncMock(return_value=row)):
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

    with patch.object(jobs_router_mod.jobs_lib, "get", AsyncMock(return_value=row)):
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
async def test_create_job_skips_ownership_check_in_single_tenant_mode():
    """WS-6B-α: user_id=None → no DB acquire even when extractor would return an int."""
    _r, request_model, pool, conn, handlers = _build_factory_with_owner_hook()

    with patch.object(jobs_router_mod.jobs_lib, "enqueue", AsyncMock(return_value="j-1")):
        result = await handlers["create_job"](
            request=MagicMock(),
            body=request_model(kind="foo.bar", payload={"paper_id": 42}),
            db_pool=pool,
            user_id=None,
        )
    assert result.job_id == "j-1"
    # Single-tenant mode must not even acquire a connection for the ownership probe.
    pool.acquire.assert_not_called()
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_create_job_403_when_paper_owned_by_other_user():
    """WS-6B-α: caller=99, paper.user_id=42 → assert_paper_ownership raises 403."""
    _r, request_model, pool, conn, _handlers = _build_factory_with_owner_hook()
    # The ownership check fetches user_id from papers — return a row owned by 42.
    conn.fetchrow.return_value = {"user_id": 42}
    handlers = _handlers

    with patch.object(jobs_router_mod.jobs_lib, "enqueue", AsyncMock()) as enqueue:
        with pytest.raises(HTTPException) as exc:
            await handlers["create_job"](
                request=MagicMock(),
                body=request_model(kind="foo.bar", payload={"paper_id": 7}),
                db_pool=pool,
                user_id=99,
            )
    assert exc.value.status_code == 403
    enqueue.assert_not_called()  # ownership failure must abort before enqueue


@pytest.mark.asyncio
async def test_create_job_skips_acquire_when_extractor_returns_none():
    """WS-6B-α: batch payload with no single paper_id → extractor returns None → no acquire."""
    _r, request_model, pool, conn, handlers = _build_factory_with_owner_hook()

    with patch.object(jobs_router_mod.jobs_lib, "enqueue", AsyncMock(return_value="j-2")):
        result = await handlers["create_job"](
            request=MagicMock(),
            # no paper_id key → extractor returns None → no ownership probe
            body=request_model(kind="foo.bar", payload={"paper_ids": [1, 2, 3]}),
            db_pool=pool,
            user_id=99,
        )
    assert result.job_id == "j-2"
    pool.acquire.assert_not_called()
    conn.fetchrow.assert_not_called()
