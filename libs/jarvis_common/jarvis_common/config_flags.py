"""Serialization and type-coercion utilities shared across services."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Interpret user_config JSONB values as booleans."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", "null", ""):
            return False
    return bool(value)


async def read_global_config_flag(db_pool: Any, key: str, *, log_label: str) -> bool:
    """Read a global (``user_id IS NULL``) ``user_config`` boolean feature flag.

    Fails CLOSED: a missing row, a failed read, or a string outside the
    recognised vocabulary all read as False. These flags gate automated work
    (nightly Pulse, LLM auto-summarization), so a value we cannot interpret must
    never switch the feature on. ``log_label`` prefixes the failure log so the
    caller's feature is identifiable.

    Deliberately does not delegate to ``coerce_bool``: that helper falls back to
    ``bool(value)`` for unrecognised strings, which is fail-OPEN and wrong here.
    """
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL", key
            )
    except Exception:
        logger.exception("%s: failed to read %s config", log_label, key)
        return False
    if row is None:
        return False
    # asyncpg JSONB auto-decodes — value may be bool directly
    value = row["value"]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)
