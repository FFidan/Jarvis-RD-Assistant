"""Unit tests for paper_ingestion.routers.account."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn


@pytest.fixture(autouse=True)
def _disable_limiter():
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


def _make_user_row() -> dict:
    return {
        "id": 1,
        "email": "old@example.com",
        "role": "user",
        "display_name": None,
        "created_at": datetime.now(UTC),
        "last_login_at": None,
    }


class _FakeURL:
    path = "/api/account"

    def replace(self, **_kwargs) -> str:
        return "http://test/account/confirm-email?token=tok"

    def __str__(self) -> str:
        return "http://test/api/account"


def _build_request(pool) -> SimpleNamespace:
    state = SimpleNamespace(user_id=1)
    app_state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=app_state)
    return SimpleNamespace(state=state, app=app, url=_FakeURL())


@pytest.mark.asyncio
async def test_email_verification_sent_false_when_smtp_raises() -> None:
    """email_verification_sent must be False when send_magic_link raises."""
    from paper_ingestion.routers.account import update_account
    from paper_ingestion.models.account import AccountUpdate

    user_row = _make_user_row()
    # fetchrow calls: (1) current user, (2) clash check → None, (3) refreshed user
    pool, conn = make_pool_and_conn(
        fetchrow_side_effects=[user_row, None, user_row],
    )
    # fetchval: cooldown check — None means no recent token, so mint proceeds.
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    request = _build_request(pool)
    body = AccountUpdate(email="new@example.com")

    with patch(
        "paper_ingestion.routers.account.send_magic_link",
        AsyncMock(side_effect=RuntimeError("SMTP failure")),
    ):
        response = await update_account(body=body, request=request, user_id=1)

    assert response.email_verification_sent is False
