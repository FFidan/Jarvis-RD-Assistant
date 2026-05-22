"""Contract tests for build_jobs_router ownership enforcement (RD-DA-001 / RD-DA-002).

Tests exercise the real ``build_jobs_router`` factory with a real DB connection
so ownership checks hit actual SQL rather than mocks.

Covered:
  A. POST /api/jobs with card.generate: user B cannot enqueue for user A's
     paper → 403 before defer_async (RD-DA-001).
  B. POST /api/jobs without a session (API-key only) → 401 (RD-DA-002).
"""

from __future__ import annotations

from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from jarvis_common.testing import SharedConnPool
from pydantic import BaseModel

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "jc-jobs-contract-test-key"


class _CardGeneratePayload(BaseModel):
    kind: Literal["card.generate"]
    paper_id: int
    deck_id: int
    max_cards: int = 5


def _card_paper_extractor(payload: dict) -> int | None:
    v = payload.get("paper_id")
    return v if isinstance(v, int) else None


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _jobs_app(contract_conn):
    """Minimal FastAPI app wired to build_jobs_router with ownership extractor."""
    from jarvis_common.jobs_router import build_jobs_router
    from jarvis_common.session_middleware import SessionMiddleware

    shared = SharedConnPool(contract_conn)

    limiter_stub = MagicMock()
    limiter_stub.enabled = False
    limiter_stub.limit = lambda _spec: lambda f: f

    router = build_jobs_router(
        service_name="contract_test",
        public_kinds=frozenset({"card.generate"}),
        get_db_pool=lambda: shared,
        limiter=limiter_stub,
        payload_schemas={"card.generate": _CardGeneratePayload},
        paper_ownership_extractor=_card_paper_extractor,
    )

    app = FastAPI()
    app.add_middleware(SessionMiddleware)
    app.include_router(router, dependencies=[])
    app.state.db_pool = shared

    yield app


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


def _authed_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# RD-DA-001: card.generate enqueue rejected when caller does not own paper
# ---------------------------------------------------------------------------


async def test_create_job_card_generate_rejects_non_owner_paper(
    contract_two_users, _jobs_app, _configure_api_key
):
    """RD-DA-001: user B cannot enqueue card.generate for user A's paper → 403.

    The paper_ownership_extractor wired into build_jobs_router must fire before
    defer_async so that cross-user paper-ID injection is blocked at enqueue time.
    """
    import jarvis_common.task_registry as task_registry

    paper_id_a = contract_two_users.paper_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        async with _authed_client(_jobs_app, contract_two_users.cookie_b) as c:
            resp = await c.post(
                "/api/jobs",
                json={
                    "kind": "card.generate",
                    "payload": {
                        "paper_id": paper_id_a,
                        "deck_id": 1,
                        "max_cards": 3,
                    },
                },
            )

    assert resp.status_code == 403, (
        f"RD-DA-001: user B got {resp.status_code} enqueueing card.generate for user A's "
        f"paper {paper_id_a} (expected 403). Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# RD-DA-002: create_job requires a browser session (no user_id → 401)
# ---------------------------------------------------------------------------


async def test_create_job_requires_session_identity(
    contract_two_users, _jobs_app, _configure_api_key
):
    """RD-DA-002: API-key-only caller (no session cookie) → 401 for create_job.

    current_user_id_strict raises 401 when request.state.user_id is absent.
    This closes the path where user_id=None bypassed paper ownership checks.
    """
    import jarvis_common.task_registry as task_registry

    paper_id_a = contract_two_users.paper_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        # No cookie — API-key only
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_jobs_app),
            base_url="http://test",
            headers={"X-API-Key": _TEST_API_KEY},
        ) as c:
            resp = await c.post(
                "/api/jobs",
                json={
                    "kind": "card.generate",
                    "payload": {
                        "paper_id": paper_id_a,
                        "deck_id": 1,
                        "max_cards": 3,
                    },
                },
            )

    assert resp.status_code == 401, (
        f"RD-DA-002: API-key-only caller got {resp.status_code} (expected 401). "
        f"Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()
