"""Paper-domain command handlers: /papers, /discover, /stats, /briefing, /next, /inbox."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot import services_client
from telegram_bot.formatters import (
    escape,
    format_morning_briefing,
    format_paper_card,
    format_pulse_card,
    format_pulse_deck_link,
    format_pulse_deck_status,
    format_review_stats,
    sanitize_user_input,
)
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import (
    get_config,
    get_http,
    get_jarvis_user_id,
)
from telegram_bot.handlers.rate_limit import rate_limit
from telegram_bot.orchestration.daily_briefing import gather_briefing_sections

logger = logging.getLogger(__name__)

#: Backend lifecycle states meaning the user has not yet acted on a Pulse card.
#: A card with no state row reads as ``None``, which the backend treats as the
#: ``inbox`` default; trashed and saved papers carry a state and drop out.
_UNACTED_CARD_STATES = frozenset({None, "inbox"})


def _feed_papers(data: object) -> list[dict[str, Any]]:
    """Return the paper rows carried by a ``/api/papers/feed`` response envelope.

    Raises
    ------
    ValueError
        When the payload is not the documented ``{"papers": [...]}`` envelope.
        An unreadable response must reach the user as a failure, never as an
        empty feed.
    """
    if not isinstance(data, dict) or not isinstance(data.get("papers"), list):
        raise ValueError("Paper feed response did not match the expected envelope")
    rows: list[dict[str, Any]] = data["papers"]
    return rows


def _feed_total(data: object) -> int | None:
    """Return the whole-view total carried by a feed envelope, or ``None`` if absent.

    The feed's ``total`` counts the view, not the requested page, so a header
    can say how many papers the stage holds. A missing total is reported as
    ``None`` so the header drops the claim rather than printing a zero.
    """
    if isinstance(data, dict) and isinstance(data.get("total"), int):
        total: int = data["total"]
        return total
    return None


def _stage_header(title: str, shown: int, total: int | None, described: str) -> str:
    """Build the one-line header naming a paper stage, what it holds, and how much.

    Parameters
    ----------
    title : str
        Stage name as the user knows it, e.g. ``"📥 <b>Inbox</b>"``.
    shown : int
        Number of papers this message actually lists.
    total : int | None
        Papers in the whole stage, or ``None`` when the backend did not say.
    described : str
        What membership of the stage means, e.g. ``"papers waiting for triage"``.

    Returns
    -------
    str
        HTML-formatted single-line header.
    """
    counted = f"{shown} of {total}" if total is not None else str(shown)
    return f"{title} — showing {counted} {described}"


# One verb per action, spelled the same on every row that offers it. "Save"
# moves a paper into the library; "Star" flags one wherever it already lives —
# the same split the web app's lifecycle actions use, so an inbox paper offers
# both rather than making Save and Star look like two names for one thing.
_SAVE_VERB = "💾 Save"
_STAR_VERB = "⭐ Star"
_TRASH_VERB = "🗑 Trash"
_READ_MORE_VERB = "📖 Read more"


def _library_keyboard(paper_id: int | str) -> InlineKeyboardMarkup:
    """/papers Library row buttons."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(_STAR_VERB, callback_data=f"paper:star:{paper_id}"),
                InlineKeyboardButton(_TRASH_VERB, callback_data=f"paper:trash:{paper_id}"),
                InlineKeyboardButton(_READ_MORE_VERB, callback_data=f"paper_detail_{paper_id}"),
            ]
        ]
    )


def _pulse_card_keyboard(paper_id: int | str) -> InlineKeyboardMarkup:
    """/next single Pulse card buttons."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(_SAVE_VERB, callback_data=f"paper:save:{paper_id}"),
                InlineKeyboardButton(_TRASH_VERB, callback_data=f"paper:trash:{paper_id}"),
                InlineKeyboardButton("🗑+👎", callback_data=f"paper:trash_reject:{paper_id}"),
            ],
            [
                InlineKeyboardButton(
                    "👍", callback_data=f"paper:feedback_pos:{paper_id}:pulse_thumbs"
                ),
                InlineKeyboardButton(
                    "👎", callback_data=f"paper:feedback_neg:{paper_id}:pulse_thumbs"
                ),
                InlineKeyboardButton(_READ_MORE_VERB, callback_data=f"paper_detail_{paper_id}"),
            ],
        ]
    )


# Decorator order: @rate_limit outer, @auth_required inner.
# Rate-limiting runs FIRST so unauthenticated floods are shed before any auth
# DB lookup occurs (auth must not run before rate-limiter).
@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def papers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/papers [query]`` — list Library papers, or search within them."""
    if update.message is None:
        return
    query = sanitize_user_input(" ".join(context.args) if context.args else "", 500)

    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required

    try:
        if query:
            data = await services_client.search_papers_feed(
                http, config, jarvis_user_id, query, limit=10
            )
        else:
            data = await services_client.fetch_papers_feed(
                http, config, jarvis_user_id, view="library", limit=10
            )
        papers = _feed_papers(data)
        total = _feed_total(data)
    except Exception:
        logger.exception("Failed to fetch library feed")
        await update.message.reply_text(
            "Failed to load library. Please try again later.",
            parse_mode="HTML",
        )
        return

    if not papers:
        if query:
            safe_query = escape(query)
            message = (
                f'No library papers match "{safe_query}". '
                f"Try /discover {safe_query} to search external sources."
            )
        else:
            message = (
                "Your Library is empty. Save papers from /inbox or /next to start building it."
            )
        await update.message.reply_text(message, parse_mode="HTML")
        return

    listed = papers[:10]
    if query:
        header = _stage_header(
            "🔎 <b>Library search</b>",
            len(listed),
            total,
            f'papers in your library matching "{escape(query)}"',
        )
    else:
        header = _stage_header(
            "📚 <b>Library</b>",
            len(listed),
            total,
            "papers you saved, are reading, or finished",
        )
    await update.message.reply_text(header, parse_mode="HTML")

    for paper in listed:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        text = format_paper_card(paper)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_library_keyboard(paper_id),
            disable_web_page_preview=True,
        )


def _paper_count(count: int) -> str:
    """Render a paper count with the matching noun, e.g. ``1 paper`` / ``3 papers``."""
    return "1 paper" if count == 1 else f"{count} papers"


def _format_discovery_result(query: str, response: dict[str, Any]) -> str:
    """Render a discovery search, stating the library write it actually performed."""
    results = response.get("results") or []
    if not results:
        return f'No external source returned a paper for "{escape(query)}". Try different terms.'

    # Report what the backend persisted, never the number of results found:
    # persistence is per paper, so a search can find papers and save none.
    saved_count = len(response.get("saved") or [])
    failed_count = len(response.get("failed") or [])
    if saved_count:
        lines = [f"Found {_paper_count(len(results))} and saved {saved_count} to your library."]
        if failed_count:
            lines.append(f"{_paper_count(failed_count)} could not be saved.")
    else:
        lines = [f"Found {_paper_count(len(results))}, but none could be saved to your library."]
    per_source_counts = response.get("per_source_counts") or {}
    found_in = [
        f"{escape(source)}: {count}" for source, count in per_source_counts.items() if count
    ]
    if found_in:
        lines.append("From " + ", ".join(found_in) + ".")
    degraded_sources = response.get("degraded_sources") or []
    if degraded_sources:
        # The response reports only which sources were skipped, not why: a source
        # lands here when it is turned off in Settings and when it fails to
        # answer. Name both causes rather than assert the rarer one.
        lines.append(
            "No results from "
            + ", ".join(escape(s) for s in degraded_sources)
            + " (turned off in Settings, or not responding)."
        )
    return "\n".join(lines)


@rate_limit(max_calls=3, window_seconds=60)
@auth_required
async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/discover <query>`` — search external sources and save what they return."""
    if update.message is None:
        return
    query = sanitize_user_input(" ".join(context.args) if context.args else "", 500)
    if not query:
        await update.message.reply_text(
            "Send a query, for example /discover protein folding. "
            "Discovery searches arXiv, Semantic Scholar, OpenAlex and PubMed, "
            "and saves what it finds to your library.",
            parse_mode="HTML",
        )
        return

    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required

    try:
        response = await services_client.search_papers(http, config, jarvis_user_id, query)
    except Exception:
        logger.exception("External paper discovery failed")
        await update.message.reply_text(
            "Discovery failed. Please try again later.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(_format_discovery_result(query, response), parse_mode="HTML")


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/stats`` — fetch and display learning statistics from the learning engine."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        stats = await services_client.fetch_stats(http, config, jarvis_user_id, timeout=15.0)
    except Exception:
        logger.exception("Failed to fetch stats")
        await update.message.reply_text("Failed to retrieve learning stats.", parse_mode="HTML")
        return

    await update.message.reply_text(format_review_stats(stats), parse_mode="HTML")


@rate_limit(max_calls=3, window_seconds=60)
@auth_required
async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/briefing`` — composite morning briefing (papers, cards, tasks, milestones)."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    user_id = get_jarvis_user_id(context)
    assert user_id is not None  # noqa: S101 — guaranteed by @auth_required

    sections = await gather_briefing_sections(http, config, user_id)
    text = format_morning_briefing(
        sections.new_papers_count,
        sections.inbox_total,
        sections.due_cards,
        sections.open_tasks,
        sections.milestones,
    )
    await update.message.reply_text(text, parse_mode="HTML")


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/next`` — surface the next Pulse card the user has not acted on.

    Cards arrive ranked, and each carries the backend's own lifecycle state for
    this user, so the command advances by skipping the cards already saved,
    read, or otherwise acted on. Nothing is remembered between calls: rating or
    saving a card through the web deck advances ``/next`` just the same.
    """
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        data = await services_client.fetch_pulse_today(http, config, jarvis_user_id)
    except Exception:
        logger.exception("Failed to fetch pulse deck for /next")
        await update.message.reply_text(
            "Failed to load next recommendation. Please try again later.",
            parse_mode="HTML",
        )
        return

    if data is None:
        await update.message.reply_text(
            "No Pulse deck yet — try /pulse_now to generate one.",
            parse_mode="HTML",
        )
        return
    if not data.cards:
        if data.empty_reason == "no_data_yet":
            message = (
                f"{format_pulse_deck_status(data)}: no papers are available yet. "
                "Try /pulse_now after your sources collect papers."
            )
        else:
            message = "No Pulse cards are available — try /pulse_now to generate a fresh deck."
        await update.message.reply_text(message, parse_mode="HTML")
        return

    # Cards arrive ranked, so the first unacted one is the highest-ranked one.
    card = next((c for c in data.cards if c.user_state in _UNACTED_CARD_STATES), None)
    if card is None:
        await update.message.reply_text(
            f"You have acted on all {data.card_count} Pulse cards for today.\n"
            f"{format_pulse_deck_link(config.jarvis_base_url)}".rstrip(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    paper_id = card.paper_id

    text = f"<b>{format_pulse_deck_status(data)}</b>\n\n{format_pulse_card(card.model_dump())}"
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_pulse_card_keyboard(paper_id),
        disable_web_page_preview=True,
    )


def _inbox_keyboard(
    paper_id: int | str, discovery_origin: str = "user_initiated"
) -> InlineKeyboardMarkup:
    """Inbox row buttons (origin-conditional feedback)."""
    primary = [
        InlineKeyboardButton(_SAVE_VERB, callback_data=f"paper:save:{paper_id}"),
        InlineKeyboardButton(_STAR_VERB, callback_data=f"paper:star:{paper_id}"),
        InlineKeyboardButton(_TRASH_VERB, callback_data=f"paper:trash:{paper_id}"),
    ]
    if discovery_origin != "user_initiated":
        primary.append(
            InlineKeyboardButton("🗑+👎", callback_data=f"paper:trash_reject:{paper_id}"),
        )
    secondary = [InlineKeyboardButton(_READ_MORE_VERB, callback_data=f"paper_detail_{paper_id}")]
    if discovery_origin != "user_initiated":
        secondary = [
            InlineKeyboardButton("👍", callback_data=f"paper:feedback_pos:{paper_id}:feed_thumbs"),
            InlineKeyboardButton("👎", callback_data=f"paper:feedback_neg:{paper_id}:feed_thumbs"),
        ] + secondary
    return InlineKeyboardMarkup([primary, secondary])


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/inbox`` — show top 10 unread inbox papers for triage."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        data = await services_client.fetch_papers_feed(
            http, config, jarvis_user_id, view="inbox", limit=10
        )
        papers = _feed_papers(data)
        total = _feed_total(data)
    except Exception:
        logger.exception("Failed to fetch inbox feed")
        await update.message.reply_text(
            "Failed to load inbox. Please try again later.",
            parse_mode="HTML",
        )
        return

    if not papers:
        await update.message.reply_text("📭 Inbox is empty — nothing to triage.", parse_mode="HTML")
        return

    listed = papers[:10]
    await update.message.reply_text(
        _stage_header("📥 <b>Inbox</b>", len(listed), total, "papers waiting for triage"),
        parse_mode="HTML",
    )

    for paper in listed:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        text = format_paper_card(paper)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_inbox_keyboard(paper_id, paper.get("discovery_origin", "user_initiated")),
            disable_web_page_preview=True,
        )
