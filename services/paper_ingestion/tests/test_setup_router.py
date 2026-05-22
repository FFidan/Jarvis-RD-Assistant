"""Tests for SetupStatusResponse hw_tier / backend extensions (Task 18)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import paper_ingestion.routers.setup as setup_router
import pytest
from jarvis_common.testing import make_pool_and_conn


@pytest.fixture(autouse=True)
def _disable_limiter():
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


def _build_request(pool: MagicMock) -> SimpleNamespace:
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app, state=state, cookies={})


@pytest.mark.asyncio
async def test_setup_status_includes_hw_fields(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")
    monkeypatch.setenv("JARVIS_LLM_BACKEND", "vllm")

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)

    assert res.hw_tier_baseline == "ge-48"
    assert res.hw_tier_current is not None
    assert res.current_backend == "vllm"
