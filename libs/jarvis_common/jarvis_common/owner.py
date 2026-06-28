"""Resolve the configured owner user for the API-key-to-session mint.

Mirrors the ``api_key_login_enabled`` env-wins-then-DB-row pattern: the env
``OWNER_USER_ID`` short-circuits; otherwise the ``owner.user_id`` system row
written by first-admin setup decides. A malformed DB row is ignored (logged)
rather than raised so a bad row can never lock out API-key login.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

OWNER_USER_ID_CONFIG_KEY = "owner.user_id"


async def resolve_owner_user_id(conn: Any) -> int | None:
    """Resolve the configured owner user id.

    Env ``OWNER_USER_ID`` wins; else the DB system row written by first-admin
    setup. Returns ``None`` when neither is set. Always re-queries (no cache):
    the api-key-session endpoint is low-frequency.
    """
    from jarvis_common.settings import get_core_settings  # noqa: PLC0415

    env_owner = get_core_settings().owner_user_id
    if env_owner is not None:
        return int(env_owner)
    row = await conn.fetchval(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        OWNER_USER_ID_CONFIG_KEY,
    )
    if row is None:
        return None
    try:
        return int(row)
    except (TypeError, ValueError):
        logger.warning("owner.user_id config row is not an integer: %r", row)
        return None
