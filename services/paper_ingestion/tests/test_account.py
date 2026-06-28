"""Unit tests for paper_ingestion.routers.account — email-change cooldown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
async def test_email_change_cooldown_suppresses_second_token() -> None:
    """Second email-change request within the cooldown window must not mint a new token.

    First call: no recent token → INSERT happens, email_verification_sent=True.
    Second call (within 30 s): recent token found → INSERT skipped, email_verification_sent=False.
    Also asserts that no second pending_email IS NOT NULL token row was minted.
    """
    from paper_ingestion.models.account import AccountUpdate
    from paper_ingestion.routers.account import update_account

    user_row = _make_user_row()
    now = datetime.now(UTC)
    recent_ts = now - timedelta(seconds=30)  # 30 s ago — within the 2-min cooldown

    # fetchrow sequence across two update_account calls (3 calls each):
    #   call 1: current user, clash→None, refreshed
    #   call 2: current user, clash→None, refreshed
    pool, conn = make_pool_and_conn(
        fetchrow_side_effects=[user_row, None, user_row, user_row, None, user_row],
    )
    # fetchval for cooldown check: first call returns None (no token yet);
    # second call returns a timestamp 30 s ago (within the 2-min window).
    conn.fetchval = AsyncMock(side_effect=[None, recent_ts])

    execute_calls: list[str] = []

    async def _tracked_execute(query, *args):
        execute_calls.append(query)

    conn.execute = AsyncMock(side_effect=_tracked_execute)

    request = _build_request(pool)
    body = AccountUpdate(email="new@example.com")

    with patch("paper_ingestion.routers.account.send_magic_link", AsyncMock()):
        resp1 = await update_account(body=body, request=request, user_id=1)
        resp2 = await update_account(body=body, request=request, user_id=1)

    # First call: token minted and email sent.
    assert resp1.email_verification_sent is True
    # Second call: within cooldown — no token, no send.
    assert resp2.email_verification_sent is False

    # Exactly one INSERT into magic_link_tokens across both calls.
    insert_calls = [q for q in execute_calls if "INSERT INTO magic_link_tokens" in q]
    assert len(insert_calls) == 1, f"Expected 1 INSERT, got {len(insert_calls)}: {insert_calls}"
