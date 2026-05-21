"""Char-tests for jarvis_common.testing factory helpers (Wave 1)."""

import pytest
from jarvis_common.testing import make_pool_and_conn, make_request
from jarvis_common.testing_embedder import _FakeEncoding, _make_embedder
from paper_ingestion.ingestion.embedder import Embedder


def test_make_request_sets_user_id_on_state() -> None:
    req = make_request(user_id=42)
    assert req.state.user_id == 42


def test_make_request_sets_role_when_provided() -> None:
    req = make_request(user_id=1, role="admin")
    assert req.state.user_role == "admin"


@pytest.mark.asyncio
async def test_make_pool_and_conn_raise_on_acquire() -> None:
    pool, _conn = make_pool_and_conn(raise_on_acquire=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        async with pool.acquire() as _:
            pass


@pytest.mark.asyncio
async def test_make_pool_and_conn_fetchrow_side_effects() -> None:
    pool, conn = make_pool_and_conn(fetchrow_side_effects=[{"r": 1}, {"r": 2}])
    assert await conn.fetchrow("any") == {"r": 1}
    assert await conn.fetchrow("any") == {"r": 2}


@pytest.mark.asyncio
async def test_testing_embedder_make_embedder_returns_embed_capable_mock() -> None:
    embedder = _make_embedder()
    # _make_embedder returns a real Embedder instance with mocked HTTP/Qdrant clients
    assert isinstance(embedder, Embedder)
    assert isinstance(embedder._encoding, _FakeEncoding)
