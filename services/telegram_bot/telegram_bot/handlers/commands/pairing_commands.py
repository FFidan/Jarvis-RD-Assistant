"""Pairing command handlers: /pair, /unpair, /whoami (Sprint A multi-tenant)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import get_config, get_db
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)


async def pair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/pair <token>`` — complete per-user Telegram pairing.

    The user must have already generated a token from the JARVIS web dashboard
    (Settings → Integrations).  The token is consumed atomically: once used it
    cannot be replayed, and expired tokens are rejected with a clear error.

    Unlike the legacy ``/start PAIR_<code>`` admin flow, this command does NOT
    require the chat to be pre-authorised (no auth_required decorator) so that
    brand-new users can pair without a chicken-and-egg problem.

    Concretely:
    - Look up ``telegram_pairing_tokens`` for the supplied token.
    - Reject if expired or already consumed.
    - Upsert ``telegram_user_pairings(user_id, chat_id, telegram_username)``.
    - Mark the token as consumed (``consumed_at = NOW()``).
    """
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "Usage: <code>/pair &lt;token&gt;</code>\n\n"
            "Generate a token from the JARVIS web dashboard under "
            "Settings → Integrations → Telegram.",
            parse_mode="HTML",
        )
        return

    token = args[0].strip()
    if not token:
        await message.reply_text(
            "Token cannot be empty. Generate one from Settings → Integrations.",
            parse_mode="HTML",
        )
        return

    db_pool = get_db(context)
    telegram_username: str | None = None
    if chat.username:
        telegram_username = chat.username

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT user_id, expires_at, consumed_at
                       FROM telegram_pairing_tokens
                       WHERE token = $1
                       FOR UPDATE""",
                    token,
                )
                if row is None:
                    await message.reply_text(
                        "Invalid or unrecognised pairing token.\n"
                        "Please generate a new one from the JARVIS dashboard."
                    )
                    return

                if row["consumed_at"] is not None:
                    await message.reply_text(
                        "This token has already been used.\n"
                        "Please generate a new one from Settings → Integrations."
                    )
                    return

                if row["expires_at"] < datetime.now(UTC):
                    # Clean up expired token opportunistically
                    await conn.execute(
                        "DELETE FROM telegram_pairing_tokens WHERE token = $1",
                        token,
                    )
                    await message.reply_text(
                        "Pairing token expired (15-minute window).\n"
                        "Please generate a new one from Settings → Integrations."
                    )
                    return

                user_id: int = int(row["user_id"])

                # Upsert the pairing — one row per user_id, UNIQUE on chat_id.
                await conn.execute(
                    """INSERT INTO telegram_user_pairings
                           (user_id, chat_id, telegram_username, paired_at)
                       VALUES ($1, $2, $3, NOW())
                       ON CONFLICT (user_id) DO UPDATE
                           SET chat_id = EXCLUDED.chat_id,
                               telegram_username = EXCLUDED.telegram_username,
                               paired_at = NOW()""",
                    user_id,
                    chat.id,
                    telegram_username,
                )

                # Mark the token as consumed so it cannot be replayed.
                await conn.execute(
                    "UPDATE telegram_pairing_tokens SET consumed_at = NOW() WHERE token = $1",
                    token,
                )

        logger.info("Telegram pairing complete: user_id=%d chat_id=%d", user_id, chat.id)
        await message.reply_text(
            "✅ Paired! You'll now receive personalised JARVIS notifications here.\n\n"
            "Use /whoami to confirm your pairing.",
        )
    except Exception:
        logger.exception("Error completing Telegram pairing for chat_id=%d", chat.id)
        await message.reply_text("Pairing failed — please try again from the dashboard.")


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def unpair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/unpair`` — remove the current user's Telegram pairing.

    Deletes the row from ``telegram_user_pairings`` keyed on ``chat_id``.
    Also purges any unconsumed pairing tokens for the user to keep the table
    tidy.  Reports success even when no pairing existed (idempotent).

    Requires the chat to be authorised (legacy owner OR multi-tenant pairing).
    """
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    db_pool = get_db(context)
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Find the user_id for this chat so we can clean up tokens too.
                user_id_row = await conn.fetchval(
                    "SELECT user_id FROM telegram_user_pairings WHERE chat_id = $1",
                    chat.id,
                )
                deleted = await conn.execute(
                    "DELETE FROM telegram_user_pairings WHERE chat_id = $1",
                    chat.id,
                )
                if user_id_row is not None:
                    await conn.execute(
                        "DELETE FROM telegram_pairing_tokens"
                        " WHERE user_id = $1 AND consumed_at IS NULL",
                        int(user_id_row),
                    )

        if deleted and deleted != "DELETE 0":
            logger.info("Telegram pairing removed: chat_id=%d", chat.id)
            await message.reply_text(
                "✅ Unpaired — you will no longer receive personal JARVIS notifications.\n\n"
                "You can re-pair at any time from Settings → Integrations."
            )
        else:
            await message.reply_text(
                "No active pairing found for this chat.\nUse /pair <token> to link your account."
            )
    except Exception:
        logger.exception("Error removing Telegram pairing for chat_id=%d", chat.id)
        await message.reply_text("Failed to remove pairing — please try again.")


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/whoami`` — show the current chat's pairing status.

    Queries ``telegram_user_pairings`` by ``chat_id``.  Works for both
    authenticated (already paired) and unauthenticated callers — the latter
    will see an "unpaired" message with instructions.

    No auth_required decorator intentionally: it's safe to call before pairing.
    """
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    db_pool = get_db(context)
    try:
        row = await db_pool.fetchrow(
            """SELECT user_id, telegram_username, paired_at
               FROM telegram_user_pairings
               WHERE chat_id = $1""",
            chat.id,
        )
    except Exception:
        logger.exception("DB error in /whoami for chat_id=%d", chat.id)
        await message.reply_text("Failed to look up pairing status — please try again.")
        return

    if row is None:
        config = get_config(context)
        # Check legacy single-tenant pairing too
        env_chat_id = getattr(config, "telegram_chat_id", None)
        is_legacy_owner = env_chat_id and chat.id == env_chat_id
        if is_legacy_owner:
            await message.reply_text(
                "This chat is paired as the <b>system owner</b> (legacy single-tenant mode).\n\n"
                "To migrate to per-user pairing, generate a token in Settings → Integrations "
                "and run /pair.",
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "This chat is <b>not paired</b> to a JARVIS account.\n\n"
                "Generate a pairing token from Settings → Integrations and run "
                "<code>/pair &lt;token&gt;</code>.",
                parse_mode="HTML",
            )
        return

    user_id: int = int(row["user_id"])
    username_part = f" (@{row['telegram_username']})" if row["telegram_username"] else ""
    paired_at = row["paired_at"]
    paired_str = paired_at.strftime("%Y-%m-%d %H:%M UTC") if paired_at else "unknown"

    await message.reply_text(
        f"✅ <b>Paired</b>{username_part}\n"
        f"JARVIS user ID: <code>{user_id}</code>\n"
        f"Chat ID: <code>{chat.id}</code>\n"
        f"Paired at: {paired_str}",
        parse_mode="HTML",
    )
