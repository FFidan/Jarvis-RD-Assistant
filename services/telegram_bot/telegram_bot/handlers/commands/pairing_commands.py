"""Pairing command handlers: /pair, /unpair, /whoami (multi-tenant)."""

from __future__ import annotations

import logging

import httpx
from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from telegram_bot.formatters import escape
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import get_config, get_platform_http
from telegram_bot.handlers.rate_limit import rate_limit
from telegram_bot.platform_client import PairingOutcome, pair_chat, resolve_pairing, unpair_chat

logger = logging.getLogger(__name__)

_PAIRING_FAILURE_MESSAGES = {
    "invalid": (
        "Invalid or unrecognised pairing code.\n"
        "Please generate a new one from the JARVIS dashboard."
    ),
    "used": (
        "This token has already been used.\nPlease generate a new one from Settings → Integrations."
    ),
    "expired": (
        "Pairing code expired (15-minute window).\n"
        "Please generate a new one from Settings → Integrations."
    ),
}


async def _reply_pairing_failure(message: Message, result: PairingOutcome) -> bool:
    """Reply for a non-success pairing outcome.

    Parameters
    ----------
    message : Message
        Telegram message that initiated pairing.
    result : PairingOutcome
        Atomic outcome returned by Platform.

    Returns
    -------
    bool
        ``True`` when a failure reply was sent.
    """
    response = _PAIRING_FAILURE_MESSAGES.get(result.outcome)
    if response is None:
        return False
    await message.reply_text(response)
    return True


async def _notify_pairing_rebound(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    prior_chat_id: int,
    new_chat_id: int,
) -> None:
    """Audit and notify when a new chat displaces a user's existing pairing."""
    logger.warning(
        "Telegram pairing rebound: user_id=%d displaced chat_id=%d → new chat_id=%d",
        user_id,
        prior_chat_id,
        new_chat_id,
    )
    # Platform persists the rebound audit event in the same operation. Telegram
    # only performs the best-effort displaced-chat notification.
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


@rate_limit(max_calls=5, window_seconds=60)
async def pair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/pair <token>`` — complete per-user Telegram pairing.

    The user must have already generated a token from the JARVIS web dashboard
    (Settings → Integrations).  The token is consumed atomically: once used it
    cannot be replayed, and expired tokens are rejected with a clear error.

    This command does NOT require the chat to be pre-authorised (no
    auth_required decorator) so that brand-new users can pair without a
    chicken-and-egg problem.

    Platform owns the token lookup, single-use transaction, pairing mutation,
    and rebound audit event. The bot receives only the bounded outcome.
    """
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    # Identity binds to chat_id; in a group every member shares it, so pairing
    # there would grant them all the paired identity. Only allow 1:1 chats.
    if chat.type != ChatType.PRIVATE:
        await message.reply_text(
            "Pairing only works in a direct 1:1 chat with the bot — "
            "open a private chat and run /pair <code> there."
        )
        return

    args = context.args or []
    token = args[0].strip() if args else ""
    if not token:
        await message.reply_text(
            "Usage: <code>/pair &lt;code&gt;</code>\n\n"
            "Generate a code from the JARVIS web dashboard under "
            "Settings → Integrations → Telegram.",
            parse_mode="HTML",
        )
        return

    config = get_config(context)
    platform_client = get_platform_http(context)
    telegram_username = chat.username or None

    try:
        result = await pair_chat(
            platform_client,
            config,
            token=token,
            chat_id=chat.id,
            telegram_username=telegram_username,
        )
        if await _reply_pairing_failure(message, result):
            return
        if result.user_id is None:
            raise RuntimeError("Platform omitted the paired user identifier")

        logger.info("Telegram pairing complete: user_id=%d chat_id=%d", result.user_id, chat.id)
        if result.prior_chat_id is not None and result.prior_chat_id != chat.id:
            await _notify_pairing_rebound(
                context,
                result.user_id,
                result.prior_chat_id,
                chat.id,
            )

        await message.reply_text(
            "✅ Paired! You'll now receive personalised JARVIS notifications here.\n\n"
            "Use /whoami to confirm your pairing.",
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            logger.exception("Platform rejected Telegram pairing for chat_id=%d", chat.id)
            await message.reply_text("Pairing failed — please try again from the dashboard.")
            return
        logger.warning(
            "Pairing rejected: chat_id=%d is already paired to another account",
            chat.id,
        )
        await message.reply_text(
            "This chat is already paired to another account.\n"
            "Have the previous owner /unpair first, then try again."
        )
    except (httpx.HTTPError, RuntimeError):
        logger.exception("Error completing Telegram pairing for chat_id=%d", chat.id)
        await message.reply_text("Pairing failed — please try again from the dashboard.")


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def unpair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/unpair`` — remove the current user's Telegram pairing.

    Platform removes the pairing and outstanding codes atomically. Reports
    success even when no pairing existed (idempotent).

    Requires the chat to be authorised (i.e. already paired).
    """
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    config = get_config(context)
    platform_client = get_platform_http(context)
    try:
        removed = await unpair_chat(platform_client, config, chat.id)
        if removed:
            logger.info("Telegram pairing removed: chat_id=%d", chat.id)
            await message.reply_text(
                "✅ Unpaired — you will no longer receive personal JARVIS notifications.\n\n"
                "You can re-pair at any time from Settings → Integrations."
            )
        else:
            await message.reply_text(
                "No active pairing found for this chat.\nUse /pair <code> to link your account."
            )
    except (httpx.HTTPError, RuntimeError):
        logger.exception("Error removing Telegram pairing for chat_id=%d", chat.id)
        await message.reply_text("Failed to remove pairing — please try again.")


@rate_limit(max_calls=5, window_seconds=60)
async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/whoami`` — show the current chat's pairing status.

    Resolves the chat through Platform. Both paired and unpaired callers may
    use the command.

    No auth_required decorator intentionally: it's safe to call before pairing.
    """
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    config = get_config(context)
    platform_client = get_platform_http(context)
    try:
        pairing = await resolve_pairing(platform_client, config, chat.id)
    except (httpx.HTTPError, RuntimeError):
        logger.exception("Platform error in /whoami for chat_id=%d", chat.id)
        await message.reply_text("Failed to look up pairing status — please try again.")
        return

    if pairing is None:
        await message.reply_text(
            "This chat is <b>not paired</b> to a JARVIS account.\n\n"
            "Generate a pairing code from Settings → Integrations and run "
            "<code>/pair &lt;code&gt;</code>.",
            parse_mode="HTML",
        )
        return

    username_part = f" (@{escape(pairing.telegram_username)})" if pairing.telegram_username else ""
    paired_str = pairing.paired_at or "unknown"

    await message.reply_text(
        f"✅ <b>Paired</b>{username_part}\nPaired since: {paired_str}",
        parse_mode="HTML",
    )
