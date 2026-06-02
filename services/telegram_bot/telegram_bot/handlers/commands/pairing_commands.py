"""Pairing command handlers: /pair, /unpair, /whoami (multi-tenant)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import asyncpg
from jarvis_common.event_log import log_event
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import get_db
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)


@rate_limit(max_calls=5, window_seconds=60)
async def pair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/pair <token>`` — complete per-user Telegram pairing.

    The user must have already generated a token from the JARVIS web dashboard
    (Settings → Integrations).  The token is consumed atomically: once used it
    cannot be replayed, and expired tokens are rejected with a clear error.

    This command does NOT require the chat to be pre-authorised (no
    auth_required decorator) so that brand-new users can pair without a
    chicken-and-egg problem.

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
                # RETURNING lets us detect whether an existing row was displaced
                # (rebound) so we can audit-log and notify the prior chat.
                #
                # The CTE captures the PRE-update chat_id before the UPSERT
                # executes.  Without it, the subquery in RETURNING would see the
                # POST-INSERT value (always == the new chat_id), making the
                # rebound branch permanently dead.
                upsert_row = await conn.fetchrow(
                    """
                    WITH prev AS (
                        SELECT chat_id
                        FROM telegram_user_pairings
                        WHERE user_id = $1
                        FOR UPDATE
                    )
                    INSERT INTO telegram_user_pairings
                           (user_id, chat_id, telegram_username, paired_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                        SET chat_id = EXCLUDED.chat_id,
                            telegram_username = EXCLUDED.telegram_username,
                            paired_at = NOW()
                    RETURNING
                        (xmax <> 0)              AS was_update,
                        (SELECT chat_id FROM prev) AS prior_chat_id
                    """,
                    user_id,
                    chat.id,
                    telegram_username,
                )
                prior_chat_id: int | None = None
                if upsert_row is not None and upsert_row["was_update"]:
                    prior_chat_id = upsert_row["prior_chat_id"]

                # Mark the token as consumed so it cannot be replayed.
                await conn.execute(
                    "UPDATE telegram_pairing_tokens SET consumed_at = NOW() WHERE token = $1",
                    token,
                )

        logger.info("Telegram pairing complete: user_id=%d chat_id=%d", user_id, chat.id)

        # --- rebound: a different chat_id just took over this user's pairing ---
        if prior_chat_id is not None and prior_chat_id != chat.id:
            logger.warning(
                "Telegram pairing rebound: user_id=%d displaced chat_id=%d → new chat_id=%d",
                user_id,
                prior_chat_id,
                chat.id,
            )
            # Audit trail in system_events (fire-and-forget — don't fail the pairing)
            try:
                await log_event(
                    pool=db_pool,
                    level="warning",
                    category="auth",
                    source="telegram_bot",
                    message="pairing.rebound",
                    context={
                        "user_id": user_id,
                        "prior_chat_id": prior_chat_id,
                        "new_chat_id": chat.id,
                    },
                )
            except Exception:
                logger.exception("Failed to log pairing.rebound audit event")
            # Notify the displaced chat (best-effort — stale/blocked chats must not fail pairing)
            try:
                await context.bot.send_message(
                    prior_chat_id,
                    text=(
                        "⚠️ Security notice: Your JARVIS account is now paired to a different "
                        "Telegram chat. If this wasn't you, please contact your administrator."
                    ),
                )
            except Exception:
                logger.warning(
                    "Could not notify prior chat_id=%d of pairing rebound (chat may be stale)",
                    prior_chat_id,
                )

        await message.reply_text(
            "✅ Paired! You'll now receive personalised JARVIS notifications here.\n\n"
            "Use /whoami to confirm your pairing.",
        )
    except asyncpg.UniqueViolationError:
        # migration 071 adds UNIQUE(chat_id) on telegram_user_pairings.
        # If user B tries to pair a chat already owned by user A, the INSERT
        # ON CONFLICT (user_id) path still violates the chat_id unique constraint.
        logger.warning(
            "Pairing rejected: chat_id=%d is already paired to another account",
            chat.id,
        )
        await message.reply_text(
            "This chat is already paired to another account.\n"
            "Have the previous owner /unpair first, then try again."
        )
    except Exception:
        logger.exception("Error completing Telegram pairing for chat_id=%d", chat.id)
        await message.reply_text("Pairing failed — please try again from the dashboard.")


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def unpair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/unpair`` — remove the current user's Telegram pairing.

    Deletes the row from ``telegram_user_pairings`` keyed on ``chat_id``.
    Also purges any unconsumed pairing tokens for the user to keep the table
    tidy.  Reports success even when no pairing existed (idempotent).

    Requires the chat to be authorised (i.e. already paired).
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


@rate_limit(max_calls=5, window_seconds=60)
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
        await message.reply_text(
            "This chat is <b>not paired</b> to a JARVIS account.\n\n"
            "Generate a pairing token from Settings → Integrations and run "
            "<code>/pair &lt;token&gt;</code>.",
            parse_mode="HTML",
        )
        return

    username_part = f" (@{row['telegram_username']})" if row["telegram_username"] else ""
    paired_at = row["paired_at"]
    paired_str = paired_at.strftime("%Y-%m-%d %H:%M UTC") if paired_at else "unknown"

    await message.reply_text(
        f"✅ <b>Paired</b>{username_part}\nPaired since: {paired_str}",
        parse_mode="HTML",
    )
