"""/api/me/export returns the caller's data only."""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app


class _FakeCursor:
    """Async-iterable over (json_str,) tuples — mimics asyncpg cursor."""

    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield (r,)

        return gen()


def _build_conn(rows_by_user: dict[int, list[str]]) -> AsyncMock:
    conn = AsyncMock()

    def cursor(sql: str, user_id: int):
        return _FakeCursor(rows_by_user.get(user_id, []))

    conn.cursor = cursor
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


def _build_pool(conn: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


@contextmanager
def _export_app(pool: MagicMock):
    """Wire the PI app to *pool* and steer the caller to user 1.

    ``export_my_data`` reads ``request.app.state.db_pool`` and resolves
    ``current_user_id_strict`` via ``Depends`` — the latter is steered through
    ``app.dependency_overrides`` (a module-symbol swap no longer reaches the route).
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            disable_limiter=True,
            dependency_overrides={
                verify_api_key: lambda: None,
                current_user_id_strict: lambda: 1,
            },
        ),
    ) as wired_app:
        yield wired_app


async def _get_export(app) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/api/me/export", headers={"X-API-Key": "test"})


@pytest.mark.real_auth
@pytest.mark.asyncio
async def test_export_returns_zip_of_caller_data() -> None:
    conn = _build_conn({1: ['{"id": 1, "title": "mine"}']})
    pool = _build_pool(conn)

    with _export_app(pool) as app:
        resp = await _get_export(app)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == ('attachment; filename="jarvis-data-export.zip"')
    body = resp.content

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        assert "papers.jsonl" in names
        assert "cards.jsonl" in names
        assert b"mine" in zf.read("papers.jsonl")


@pytest.mark.real_auth
@pytest.mark.asyncio
async def test_export_excludes_other_users_data() -> None:
    # Only user 2 has rows; caller is user 1 → must get empty papers.jsonl.
    conn = _build_conn({2: ['{"id": 9, "title": "not yours"}']})
    pool = _build_pool(conn)

    with _export_app(pool) as app:
        resp = await _get_export(app)

    assert resp.status_code == 200, resp.text
    body = resp.content
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert zf.read("papers.jsonl") == b""
        assert b"not yours" not in zf.read("papers.jsonl")


def test_export_my_data_is_rate_limited() -> None:
    """DOS-2: export_my_data must carry a @limiter.limit decorator (5/minute).

    slowapi's limiter.limit() wraps the function via functools.wraps, which
    sets __wrapped__ on the outer callable.  We also verify the inner function
    name is registered in the limiter's route-limit table.
    """
    import paper_ingestion.routers.settings as settings_router
    from paper_ingestion.deps import limiter

    handler = settings_router.export_my_data
    assert hasattr(handler, "__wrapped__"), (
        "export_my_data is missing @limiter.limit — __wrapped__ not set (DOS-2)"
    )
    # slowapi tracks limits keyed by the function's qualified name
    qualname = handler.__wrapped__.__qualname__
    module = handler.__wrapped__.__module__
    key = f"{module}.{qualname}"
    assert key in limiter._route_limits, (
        f"export_my_data ({key!r}) not registered in limiter._route_limits — DOS-2 not satisfied"
    )
