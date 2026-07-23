"""Resolve and validate the deployment owner identity."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

OWNER_USER_ID_CONFIG_KEY = "owner.user_id"
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    """Resolved instance-owner configuration.

    Attributes
    ----------
    source : str
        Authoritative configuration layer: ``"environment"``, ``"database"``,
        or ``"none"`` when neither layer provides an owner.
    state : str
        Resolution outcome: ``"valid"``, ``"missing"``, ``"invalid_value"``,
        ``"missing_or_deleted_user"``, or ``"non_admin_user"``.
    user_id : int | None
        Parsed target ID when the configured value is a positive integer,
        including invalid targets that reference a missing or non-admin user.
    """

    source: str
    state: str
    user_id: int | None = None

    @property
    def is_valid(self) -> bool:
        return self.state == "valid" and self.user_id is not None


def _parse_owner_id(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not _POSITIVE_INTEGER.fullmatch(text):
        return None
    return int(text)


async def resolve_owner_identity(conn: Any) -> OwnerIdentity:
    """Resolve the authoritative owner setting against the live user table.

    Parameters
    ----------
    conn : Any
        Async database connection exposing ``fetchval`` and ``fetchrow``.

    Returns
    -------
    OwnerIdentity
        Source, state, and parsed target ID. Invalid configuration is returned
        as a typed state rather than silently falling back to another admin.

    Notes
    -----
    The environment is authoritative when nonblank. Otherwise the deployment-
    wide database row is used. Every parsed target is checked against the live
    users table so callers never mistake a stale, deleted, or non-admin row for
    a usable recovery identity. Database and settings errors propagate so each
    caller can choose its own fail-closed response.
    """
    from jarvis_common.settings import get_core_settings  # noqa: PLC0415

    env_raw = get_core_settings().owner_user_id
    if env_raw is not None:
        source = "environment"
        raw = env_raw
    else:
        raw = await conn.fetchval(
            "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
            OWNER_USER_ID_CONFIG_KEY,
        )
        if raw is None:
            return OwnerIdentity(source="none", state="missing")
        source = "database"

    user_id = _parse_owner_id(raw)
    if user_id is None:
        logger.warning("%s owner id is not a positive integer", source)
        return OwnerIdentity(source=source, state="invalid_value")

    user = await conn.fetchrow(
        "SELECT id, role, deleted_at FROM users WHERE id = $1",
        user_id,
    )
    if user is None or user["deleted_at"] is not None:
        return OwnerIdentity(
            source=source,
            state="missing_or_deleted_user",
            user_id=user_id,
        )
    if user["role"] != "admin":
        return OwnerIdentity(source=source, state="non_admin_user", user_id=user_id)
    return OwnerIdentity(source=source, state="valid", user_id=user_id)


async def resolve_owner_user_id(conn: Any) -> int | None:
    """Compatibility adapter returning an ID only for a valid live admin."""
    identity = await resolve_owner_identity(conn)
    return identity.user_id if identity.is_valid else None
