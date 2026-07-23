"""Telegram bootstrap helper for the paper_ingestion service.

Called during lifespan startup to cache the bot username so the setup wizard
can build pairing deep-links without hitting the Telegram API on every request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from jarvis_common.maintenance import outbound_quarantine_active
from jarvis_common.settings import get_secrets_settings

logger = logging.getLogger(__name__)


async def refresh_telegram_bot_username(db_pool, http_client: httpx.AsyncClient) -> None:
    """Call Telegram ``getMe`` and cache the bot username in ``user_config``.

    No-op if ``TELEGRAM_BOT_TOKEN`` is unset, if the cached entry is fresh
    (<24h old), while restored credentials are quarantined, or if the API call
    fails. Never raises: the lifespan hook must stay resilient to database,
    network, and token errors.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Pool used to read and update the non-secret username cache.
    http_client : httpx.AsyncClient
        Lifespan-owned client used for Telegram ``getMe``.
    """
    if outbound_quarantine_active():
        logger.info("skip Telegram username refresh: outbound quarantine awaiting review")
        return

    _token_secret = get_secrets_settings().telegram_bot_token
    token = _token_secret.get_secret_value() if _token_secret else ""
    if not token:
        return

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config "
                "WHERE key = 'telegram.bot_username' AND user_id IS NULL"
            )
    except Exception:
        logger.warning("telegram.bot_username lookup failed", exc_info=True)
        return

    now = datetime.now(UTC)
    if row is not None:
        value = row["value"]
        if isinstance(value, dict):
            set_at_raw = value.get("set_at")
            try:
                if isinstance(set_at_raw, str):
                    set_at = datetime.fromisoformat(set_at_raw.replace("Z", "+00:00"))
                    if set_at.tzinfo is None:
                        set_at = set_at.replace(tzinfo=UTC)
                    if now - set_at < timedelta(hours=24) and value.get("username"):
                        return
            except ValueError as _exc:
                logger.debug("set_at parse failed (stale/malformed) — refreshing", exc_info=_exc)
                pass  # stale or malformed -> refresh

    try:
        if outbound_quarantine_active():
            logger.info("skip Telegram username refresh: outbound quarantine awaiting review")
            return
        resp = await http_client.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=5.0,
        )
    except Exception:
        logger.warning("Telegram getMe request failed", exc_info=True)
        return

    if resp.status_code != 200:
        logger.warning("Telegram getMe returned HTTP %s", resp.status_code)
        return

    try:
        payload = resp.json()
    except Exception:
        logger.warning("Telegram getMe returned non-JSON payload", exc_info=True)
        return

    if not payload.get("ok"):
        logger.warning("Telegram getMe ok=false: %s", payload.get("description"))
        return
    username = payload.get("result", {}).get("username")
    if not isinstance(username, str) or not username:
        logger.warning("Telegram getMe result missing username")
        return

    # Pass the dict directly — asyncpg's JSONB codec handles serialisation.
    # json.dumps() here would double-encode the value. (WEB-C01)  # nolint:jsonb-double-encode
    cache_value = {"username": username, "set_at": now.isoformat()}
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_config (user_id, key, value) VALUES (NULL, $1, $2::jsonb)
                ON CONFLICT (user_id, key) DO UPDATE SET value = $2::jsonb, updated_at = NOW()""",
                "telegram.bot_username",
                cache_value,
            )
        logger.info("Telegram bot username cached as @%s", username)
    except Exception:
        logger.warning("Failed to persist telegram.bot_username", exc_info=True)
