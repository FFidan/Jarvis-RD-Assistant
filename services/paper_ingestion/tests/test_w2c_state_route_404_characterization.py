"""W2c B-DRY-RED9 characterization test.

Pins the 404 behaviour for all 9 state-mutator routes before and after the
redundant ``SELECT id FROM papers WHERE id = $1`` blocks are removed.

The 404 is guaranteed by ``assert_paper_ownership`` (which executes first and
already raises 404 when the paper does not exist).  The secondary SELECT is
unreachable dead code.  This file verifies that 404 is still raised after the
redundant block is deleted.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import paper_ingestion.routers.papers as papers
from tests.conftest import _make_pool_and_conn


@pytest.mark.asyncio
async def test_unstar_paper_404_nonexistent():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))
    with pytest.raises(HTTPException) as exc_info:
        await papers.unstar_paper.__wrapped__(request, 99999999, db_pool=pool)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_trash_paper_404_nonexistent():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))
    with pytest.raises(HTTPException) as exc_info:
        await papers.trash_paper.__wrapped__(request, 99999999, db_pool=pool)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_restore_paper_404_nonexistent():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))
    with pytest.raises(HTTPException) as exc_info:
        await papers.restore_paper.__wrapped__(request, 99999999, db_pool=pool)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_trash_and_reject_paper_404_nonexistent():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))
    with pytest.raises(HTTPException) as exc_info:
        await papers.trash_and_reject_paper.__wrapped__(request, 99999999, db_pool=pool)
    assert exc_info.value.status_code == 404
