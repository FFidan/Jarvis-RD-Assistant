"""Live database contracts for Platform-owned Telegram pairing state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from jarvis_common.testing_contract_apps import make_contract_client

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_new_pairing_code_replaces_an_outstanding_code(
    contract_two_users: Any,
    contract_conn: Any,
    _platform_app_with_pool: Any,
    _configure_api_key: str,
) -> None:
    """A new code leaves exactly one outstanding code for the caller.

    # Verified: services/platform_api/platform_api/routers/telegram.py:54
    """
    user_id = contract_two_users.user_a_id
    await contract_conn.execute(
        """INSERT INTO telegram_pairing_tokens (token, user_id, expires_at)
           VALUES ($1, $2, $3)""",
        "old-code",
        user_id,
        datetime.now(UTC) + timedelta(minutes=5),
    )

    async with make_contract_client(
        _platform_app_with_pool,
        contract_two_users.cookie_a,
    ) as client:
        response = await client.post("/api/telegram/pair-token")

    assert response.status_code == 200, response.text
    issued_token = response.json()["token"]
    rows = await contract_conn.fetch(
        """SELECT token, user_id, consumed_at
           FROM telegram_pairing_tokens
           WHERE user_id = $1""",
        user_id,
    )
    assert [row["token"] for row in rows] == [issued_token]
    assert rows[0]["user_id"] == user_id
    assert rows[0]["consumed_at"] is None


async def test_remove_pairing_deletes_mapping_and_outstanding_codes(
    contract_two_users: Any,
    contract_conn: Any,
    _platform_app_with_pool: Any,
    _configure_api_key: str,
) -> None:
    """Removing a pairing clears all Platform-owned state for that user.

    # Verified: services/platform_api/platform_api/routers/telegram.py:128
    """
    user_id = contract_two_users.user_a_id
    await contract_conn.execute(
        """INSERT INTO telegram_user_pairings
               (user_id, chat_id, telegram_username, paired_at)
           VALUES ($1, $2, $3, NOW())""",
        user_id,
        8801,
        "contract-user",
    )
    await contract_conn.execute(
        """INSERT INTO telegram_pairing_tokens (token, user_id, expires_at)
           VALUES ($1, $2, $3)""",
        "pending-code",
        user_id,
        datetime.now(UTC) + timedelta(minutes=5),
    )

    async with make_contract_client(
        _platform_app_with_pool,
        contract_two_users.cookie_a,
    ) as client:
        response = await client.delete("/api/telegram/pairing")

    assert response.status_code == 204, response.text
    pairing_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM telegram_user_pairings WHERE user_id = $1",
        user_id,
    )
    token_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM telegram_pairing_tokens WHERE user_id = $1",
        user_id,
    )
    assert pairing_count == 0
    assert token_count == 0
