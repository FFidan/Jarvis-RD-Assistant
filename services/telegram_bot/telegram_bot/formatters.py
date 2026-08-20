"""Telegram message formatting utilities.

All formatters produce HTML-formatted strings for use with parse_mode='HTML'.
Telegram HTML supports: <b>, <i>, <code>, <a href="...">, <pre>, <s>, <u>.
Maximum message length is 4096 characters.
"""

import html
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from telegram_bot.command_catalog import COMMAND_CATALOG
from telegram_bot.pulse_contract import PulseDeck
from telegram_bot.vocabulary import is_not_done, project_status_emoji, project_status_label

MAX_MESSAGE_LENGTH = 4096
TRUNCATION_HEADROOM = 100


_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Matches BIDI control characters (U+202A–U+202E, U+2066–U+2069) and
# zero-width/invisible characters (U+200B–U+200F including LRM/RLM, U+FEFF, etc.)
_BIDI_ZW_RE = re.compile(r"[‪-‮⁦-⁩​-‏﻿]")


def safe_url(url: str) -> str:
    """Sanitize URL — only allow safe schemes, then HTML-escape for href attributes.

    Parameters
    ----------
    url : str
        The URL to sanitize.

    Returns
    -------
    str
        The HTML-escaped URL if scheme is allowed, ``'#'`` otherwise.
        Returns ``''`` for empty input (no URL to display).
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            return "#"
        return html.escape(url, quote=True)
    except ValueError:
        return "#"


def escape(text: str | None) -> str:
    """Escape HTML special characters for Telegram, stripping BIDI/zero-width chars.

    Parameters
    ----------
    text : str or None
        Text to escape. ``None`` is treated as an empty string.

    Returns
    -------
    str
        HTML-escaped text safe for use in Telegram HTML messages.
    """
    if text is None:
        return ""
    text = _BIDI_ZW_RE.sub("", str(text))
    return html.escape(text)


def strip_bidi(text: str) -> str:
    """Remove BIDI control and zero-width characters from a string.

    Parameters
    ----------
    text : str
        Raw input string.

    Returns
    -------
    str
        String with all BIDI/zero-width characters removed.
    """
    return _BIDI_ZW_RE.sub("", text)


def sanitize_user_input(text: str, max_len: int) -> str:
    """Strip BIDI/zero-width chars and enforce a maximum length on user input.

    Parameters
    ----------
    text : str
        Raw user-supplied string.
    max_len : int
        Maximum number of characters to keep (applied after stripping).

    Returns
    -------
    str
        Cleaned, length-capped string safe for use in queries and display.
    """
    return _BIDI_ZW_RE.sub("", text)[:max_len]


# HTML tags this module documents as supported (module docstring above).
_OPEN_TAG_RE = re.compile(r"<(b|i|u|s|a|code|pre)(?:\s[^>]*)?>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"</(b|i|u|s|a|code|pre)>", re.IGNORECASE)


def _unclosed_tag_closers(text: str) -> str:
    """Return closing tags (innermost-first) for spanning tags left open.

    Scans *text* for this module's supported spanning tags and returns the
    closers needed to balance whatever is still open at the end, e.g.
    ``<b><i>`` yields ``</i></b>`` (LIFO, matching nesting order).

    Parameters
    ----------
    text : str
        Text to scan (already cut to its final length).

    Returns
    -------
    str
        Concatenated closing tags, or ``''`` if nothing is left open.
    """
    stack: list[str] = []
    pos = 0
    while pos < len(text):
        open_match = _OPEN_TAG_RE.search(text, pos)
        close_match = _CLOSE_TAG_RE.search(text, pos)
        if open_match and (not close_match or open_match.start() <= close_match.start()):
            stack.append(open_match.group(1).lower())
            pos = open_match.end()
        elif close_match:
            tag_name = close_match.group(1).lower()
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx] == tag_name:
                    stack.pop(idx)
                    break
            pos = close_match.end()
        else:
            break
    return "".join(f"</{name}>" for name in reversed(stack))


def truncate(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
    """Truncate text to fit Telegram's message limit.

    Parameters
    ----------
    text : str
        Text to truncate.
    max_length : int
        Maximum allowed length (default 4096).

    Returns
    -------
    str
        Truncated text with ellipsis if needed. Any spanning tag left open
        by the cut is closed (innermost-first) before the marker, so the
        result is always self-contained valid HTML.
    """
    limit = max_length - TRUNCATION_HEADROOM
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    # Back up before a partially-cut HTML tag: a trailing "<" without a
    # matching ">" means the cut landed inside a tag such as "</b>" or
    # '<a href="...">', which Telegram's HTML parser rejects with a 400.
    lt_pos = truncated.rfind("<")
    if lt_pos != -1 and ">" not in truncated[lt_pos:]:
        truncated = truncated[:lt_pos]
    # Likewise back up before a partially-cut HTML entity (e.g. "&amp").
    amp_pos = truncated.rfind("&")
    if amp_pos != -1 and ";" not in truncated[amp_pos:]:
        truncated = truncated[:amp_pos]
    closers = _unclosed_tag_closers(truncated)
    return truncated + closers + "\n\n<i>... (truncated)</i>"


#: How many rows a listing command sends before it stops and says how many
#: the stage actually holds. Shared so a listing cannot silently truncate.
LISTING_ROWS = 10


def stage_header(title: str, shown: int, total: int | None, described: str) -> str:
    """Build the one-line header naming a listing, what it holds, and how much.

    Parameters
    ----------
    title : str
        Stage name as the user knows it, e.g. ``"📥 <b>Inbox</b>"``.
    shown : int
        Number of rows this message actually lists.
    total : int | None
        Rows in the whole stage, or ``None`` when the backend did not say.
    described : str
        What membership of the stage means, e.g. ``"papers waiting for triage"``.

    Returns
    -------
    str
        HTML-formatted single-line header.
    """
    counted = f"{shown} of {total}" if total is not None else str(shown)
    return f"{title} — showing {counted} {described}"


def _format_authors(authors: list[str], max_display: int = 3) -> str:
    """Format author list, showing first N + 'et al.' if needed."""
    if not authors:
        return "<i>Unknown authors</i>"
    escaped = [escape(a) for a in authors[:max_display]]
    result = ", ".join(escaped)
    if len(authors) > max_display:
        result += f" <i>et al.</i> ({len(authors)} total)"
    return result


def confidence_badge(confidence: str) -> str:
    """Return a coloured confidence indicator string.

    Parameters
    ----------
    confidence : str
        Confidence level key: ``"HIGH"``, ``"MEDIUM"``, or ``"LOW"``.

    Returns
    -------
    str
        Emoji-prefixed label (e.g. ``"🟢 HIGH"``), or the HTML-escaped raw
        value when the key is unrecognised.
    """
    badges = {
        "HIGH": "🟢 HIGH",
        "MEDIUM": "🟡 MEDIUM",
        "LOW": "🔴 LOW",
    }
    return badges.get(confidence, escape(str(confidence)))


def format_paper_card(paper: dict) -> str:
    """Format a paper as a compact card for list views.

    Parameters
    ----------
    paper : dict
        Paper record from the database.

    Returns
    -------
    str
        HTML-formatted paper card.
    """
    title = escape(paper.get("title", "Untitled"))
    authors = _format_authors(paper.get("authors", []))
    date_str = ""
    if paper.get("published_date"):
        date_str = f"\n📅 {escape(str(paper['published_date']))}"
    source = escape(paper.get("source_type", ""))
    url = safe_url(paper.get("url", ""))

    tldr = ""
    if paper.get("tldr"):
        tldr = f"\n💡 <b><i>{escape(paper['tldr'])}</i></b>"

    summary = ""
    if paper.get("summary_brief"):
        summary = f"\n\n{escape(paper['summary_brief'][:300])}"

    return truncate(
        f"📄 <b>{title}</b>\n"
        f"👤 {authors}{date_str}\n"
        f"📦 {source}"
        f"{tldr}"
        f"{summary}\n"
        f'🔗 <a href="{url}">Open paper</a>'
    )


def format_pulse_card(card: dict) -> str:
    """Format a Pulse deck card (PulseCardResponse shape) for /next display.

    Parameters
    ----------
    card : dict
        A single card from ``/api/pulse/today`` (``PulseCardResponse`` shape).
        Expected keys: paper_title, paper_authors, paper_url, score,
        reasoning, rank.

    Returns
    -------
    str
        HTML-formatted Pulse card for Telegram.
    """
    title = escape(card.get("paper_title") or "Untitled")
    authors = _format_authors(card.get("paper_authors") or [])
    score = card.get("score")
    score_str = f"{score:.2f}" if isinstance(score, float) else str(score or "–")
    rank = card.get("rank")
    rank_str = f"#{rank}" if rank is not None else ""
    reasoning = card.get("reasoning") or ""
    url = safe_url(card.get("paper_url") or "")

    reasoning_part = f"\n\n💬 {escape(reasoning[:300])}" if reasoning else ""
    link_part = f'\n🔗 <a href="{url}">Open paper</a>' if url else ""

    verified = card.get("reasoning_verified")
    confidence = card.get("reasoning_confidence")
    if verified is False or confidence == "UNVERIFIED":
        evidence_part = "\nEvidence check: Unverified"
    elif verified is True and confidence in {"HIGH", "MEDIUM", "LOW"}:
        evidence_part = f"\nEvidence confidence: {str(confidence).title()}"
    elif verified is True:
        evidence_part = "\nEvidence check: Verified"
    elif confidence in {"HIGH", "MEDIUM", "LOW"}:
        evidence_part = f"\nEvidence confidence: {str(confidence).title()}"
    else:
        evidence_part = "\nEvidence check: Not reported"

    return truncate(
        f"⚡ <b>Pulse {rank_str}</b> · Score {score_str}\n"
        f"📄 <b>{title}</b>\n"
        f"👤 {authors}"
        f"{reasoning_part}"
        f"{evidence_part}"
        f"{link_part}"
    )


def format_pulse_deck_status(deck: PulseDeck) -> str:
    """Describe Pulse freshness and degradation without exposing backend diagnostics."""
    try:
        deck_date = datetime.strptime(deck.deck_date, "%Y-%m-%d").strftime("%B %d")
    except (TypeError, ValueError):
        deck_date = "the reported date"

    if deck.is_stale:
        age = deck.stale_age_days
        age_label = f"{age} day old" if age == 1 else f"{age} days old"
        status = f"Earlier Pulse from {deck_date} ({age_label})"
    else:
        status = f"Current Pulse for {deck_date}"

    if deck.degraded_reason:
        status += "\nRanking used reduced signals; some scoring inputs were unavailable."
    return status


def format_pulse_deck_scope(deck: PulseDeck, shown: int, base_url: str | None) -> str:
    """Say how much of the deck this message carries and where the rest lives.

    Parameters
    ----------
    deck : PulseDeck
        Deck being delivered. ``card_count`` is the authoritative total.
    shown : int
        Number of cards this message actually delivers.
    base_url : str or None
        Public dashboard base URL, or ``None`` when none is configured.
        Telegram cannot render a relative href, so the deck link is omitted
        rather than emitted as a dead relative link.

    Returns
    -------
    str
        One line naming the delivered slice and, when possible, linking the
        full deck.
    """
    line = f"Top {shown} of {deck.card_count}"
    link = format_pulse_deck_link(base_url)
    return f"{line} — {link}" if link else line


def format_pulse_deck_link(base_url: str | None) -> str:
    """Link to the web deck, or an empty string when no public URL is configured.

    Parameters
    ----------
    base_url : str or None
        Public dashboard base URL. Telegram cannot render a relative href, so
        an unset base URL yields no link rather than a dead one.

    Returns
    -------
    str
        An anchor element, or ``''``.
    """
    if not base_url:
        return ""
    return f'<a href="{safe_url(f"{base_url}/pulse")}">See the full deck</a>'


def format_paper_detail(paper: dict, summary: dict | None = None) -> str:
    """Format a detailed paper view with summary and findings.

    Parameters
    ----------
    paper : dict
        Paper record from the database.
    summary : dict or None
        Paper summary record, if available.

    Returns
    -------
    str
        HTML-formatted detailed paper view.
    """
    title = escape(paper.get("title", "Untitled"))
    authors = _format_authors(paper.get("authors", []))
    date_str = escape(str(paper.get("published_date", "")))
    url = safe_url(paper.get("url", ""))

    lines = [
        f"📄 <b>{title}</b>\n",
        f"👤 {authors}",
        f"📅 {date_str}",
        f'🔗 <a href="{url}">Open paper</a>',
    ]

    if summary:
        confidence = confidence_badge(summary.get("confidence", ""))
        lines.append(f"\n📊 Confidence: {confidence}")

        if summary.get("summary_detailed"):
            lines.append(f"\n<b>Summary:</b>\n{escape(summary['summary_detailed'][:800])}")

        findings = summary.get("key_findings", [])
        if findings:
            lines.append("\n<b>Key Findings:</b>")
            for i, f in enumerate(findings[:5], 1):
                finding_text = escape(f.get("finding", ""))
                quote = f.get("quote", "")
                page = f.get("page_number", "?")
                quote_html = escape(quote[:150])
                page_html = escape(str(page))
                lines.append(f'{i}. {finding_text}\n   <i>"...{quote_html}..."</i> (p.{page_html})')

    return truncate("\n".join(lines))


def format_card_front(card: dict) -> str:
    """Format flashcard front (question side).

    Parameters
    ----------
    card : dict
        Card record from the database.

    Returns
    -------
    str
        HTML-formatted card front.
    """
    card_type = card.get("card_type", "concept")
    type_badges = {
        "concept": "💡 Concept",
        "quote": "📝 Quote",
        "method": "🔧 Method",
        "comparison": "⚖️ Comparison",
    }
    badge = type_badges.get(card_type, escape(card_type))
    front = escape(card.get("front", ""))
    return f"<b>{badge}</b>\n\n{front}"


def format_card_back(card: dict) -> str:
    """Format flashcard back (answer side) with evidence.

    Parameters
    ----------
    card : dict
        Card record from the database.

    Returns
    -------
    str
        HTML-formatted card back.
    """
    back = escape(card.get("back", ""))
    evidence = card.get("evidence", {})
    lines = [f"<b>Answer:</b>\n{back}"]

    quote = evidence.get("quote", "")
    if quote:
        lines.append(f'\n<i>"...{escape(quote[:200])}..."</i>')
    page = evidence.get("page_number")
    if page:
        lines.append(f"📖 Page {escape(str(page))}")

    return "\n".join(lines)


def format_review_stats(stats: dict) -> str:
    """Format retention statistics from the learning engine.

    Parameters
    ----------
    stats : dict
        Stats response from /api/stats.

    Returns
    -------
    str
        HTML-formatted stats overview.
    """
    total = stats.get("total_cards", 0)
    due = stats.get("due_now", 0)
    reviewed_today = stats.get("reviewed_today", 0)
    retention = stats.get("average_retention", 0)
    streak = stats.get("streak_days", 0)

    return (
        "📊 <b>Learning Stats</b>\n\n"
        f"📚 Total cards: <b>{total}</b>\n"
        f"⏰ Due now: <b>{due}</b>\n"
        f"✅ Reviewed today: <b>{reviewed_today}</b>\n"
        f"🎯 Retention rate: <b>{retention:.0f}%</b>\n"
        f"🔥 Streak: <b>{streak}</b> days"
    )


#: How many rows a listed section shows before it summarizes the remainder.
_LISTED_SECTION_ROWS = 5


def _remaining_line(total: int, shown: int) -> list[str]:
    """Return a single "and N more" line when a section listed only part of its rows."""
    remaining = total - shown
    return [f"  … and {remaining} more"] if remaining > 0 else []


def _count_line(icon: str, count: int | None, counted: str, unavailable: str) -> str:
    """Render one briefing count, or say the number could not be read.

    A ``None`` count means the gather behind it failed. Rendering that as a
    zero would tell the reader there is nothing to do, when the truth is that
    nothing is known — so the outage is stated instead.
    """
    if count is None:
        return f"{icon} {unavailable}"
    return f"{icon} <b>{count}</b> {counted}"


def format_morning_briefing(
    new_papers_count: int | None,
    inbox_total: int | None,
    due_cards: int | None,
    open_tasks: list[dict] | None,
    milestones: list[dict] | None,
) -> str:
    """Format the combined morning briefing message.

    Every count names the window it was measured over and the view it was
    measured on, so a number in the briefing means the same thing as the
    matching number on the web.

    Parameters
    ----------
    new_papers_count : int | None
        Papers added to the caller's library since midnight UTC, or ``None``
        when that count could not be read.
    inbox_total : int | None
        Papers currently in the Inbox view, whenever they arrived, or ``None``
        when that count could not be read.
    due_cards : int | None
        Flashcards whose review is due as of now, or ``None`` when that count
        could not be read.
    open_tasks : list[dict] | None
        Tasks that are not done, under the same rule the My Day view applies,
        or ``None`` when the list could not be read. An empty list renders as
        no section; ``None`` says so, because an absent section reads as
        "nothing outstanding".
    milestones : list[dict] | None
        Milestones with a deadline in the next 7 days, or ``None`` when the
        list could not be read.

    Returns
    -------
    str
        HTML-formatted morning briefing.
    """
    now = datetime.now(UTC)
    lines = [f"☀️ <b>Morning Briefing</b> — {now.strftime('%A, %B %d')}\n"]

    lines.append(
        _count_line(
            "📄",
            new_papers_count,
            "papers added to your library since midnight UTC",
            "Papers added to your library since midnight UTC are unavailable right now",
        )
    )
    lines.append(
        _count_line(
            "📥",
            inbox_total,
            "waiting in your inbox",
            "Your inbox count is unavailable right now",
        )
    )
    lines.append(
        _count_line(
            "📚",
            due_cards,
            "cards due for review right now",
            "Cards due for review are unavailable right now",
        )
    )

    if open_tasks is None:
        lines.append("\n📋 Your open tasks are unavailable right now")
    elif open_tasks:
        lines.append(
            f"\n📋 <b>Open tasks ({len(open_tasks)}):</b> "
            "<i>to do, in progress or blocked — the same rule as My Day</i>"
        )
        for t in open_tasks[:_LISTED_SECTION_ROWS]:
            title = escape(t.get("title", ""))
            project = escape(t.get("project_name", ""))
            lines.append(f"  • {title}" + (f" <i>({project})</i>" if project else ""))
        lines.extend(_remaining_line(len(open_tasks), _LISTED_SECTION_ROWS))

    if milestones is None:
        lines.append("\n🎯 Milestones due in the next 7 days are unavailable right now")
    elif milestones:
        lines.append(f"\n🎯 <b>Milestones due in the next 7 days ({len(milestones)}):</b>")
        for m in milestones[:_LISTED_SECTION_ROWS]:
            name = escape(m.get("name", ""))
            deadline = m.get("deadline", "")
            project = escape(m.get("project_name", ""))
            if isinstance(deadline, datetime):
                days_left = (deadline.date() - now.date()).days
                deadline_str = f"{days_left}d left"
            else:
                deadline_str = escape(str(deadline))
            lines.append(f"  • {name} ({deadline_str}) <i>{project}</i>")
        lines.extend(_remaining_line(len(milestones), _LISTED_SECTION_ROWS))

    return truncate("\n".join(lines))


def format_project_status(project: dict, tasks: list[dict], milestones: list[dict]) -> str:
    """Format a project overview.

    Parameters
    ----------
    project : dict
        Project record, as returned by ``GET /api/projects/{id}``: it carries
        ``paper_count`` and ``open_question_count`` alongside the row columns.
    tasks : list[dict]
        Tasks for this project.
    milestones : list[dict]
        Milestones for this project.

    Returns
    -------
    str
        HTML-formatted project status.
    """
    name = escape(project.get("name", ""))
    status = project.get("status", "active")
    description = project.get("description", "") or ""

    done_count = sum(1 for t in tasks if t.get("status") == "done")
    total_tasks = len(tasks)
    progress = f"{done_count} of {total_tasks} done" if total_tasks else "No tasks"

    badge = f"{project_status_emoji(status)} ".lstrip()
    label = escape(project_status_label(status))
    lines = [f"{badge}<b>{name}</b>" + (f" — {label}" if label else "")]
    if description:
        lines.append(f"{escape(description[:200])}")
    lines.append(f"\n📋 Tasks: {progress}")
    lines.append(f"📄 Linked papers: {int(project.get('paper_count') or 0)}")
    lines.append(f"❓ Open questions: {int(project.get('open_question_count') or 0)}")

    if project.get("deadline"):
        lines.append(f"📅 Deadline: {escape(str(project['deadline']))}")

    if milestones:
        lines.append(f"\n🎯 <b>Milestones ({len(milestones)}):</b>")
        for m in milestones[:_LISTED_SECTION_ROWS]:
            done = "✅" if m.get("completed") else "⬜"
            lines.append(f"  {done} {escape(m.get('name', ''))}")

    # Same not-done rule as My Day and the briefing, rather than in-progress only.
    open_tasks = [t for t in tasks if is_not_done(t)]
    if open_tasks:
        lines.append(f"\n🔨 <b>Open tasks ({len(open_tasks)}):</b>")
        for t in open_tasks[:_LISTED_SECTION_ROWS]:
            lines.append(f"  • {escape(t.get('title', ''))}")
        lines.extend(_remaining_line(len(open_tasks), _LISTED_SECTION_ROWS))

    return truncate("\n".join(lines))


def format_weekly_digest(digest: dict) -> str:
    """Format weekly digest for Telegram HTML message.

    Parameters
    ----------
    digest : dict
        Digest data from /api/digest/weekly endpoint.

    Returns
    -------
    str
        HTML-formatted Telegram message.
    """
    total = digest.get("total_papers", 0)
    lines = [f"\U0001f4f0 <b>Weekly Research Digest</b> ({total} papers)\n"]

    for topic in digest.get("topics", []):
        name = escape(topic["name"])
        count = topic["paper_count"]
        lines.append(f"\n\U0001f4c2 <b>{name}</b> ({count} papers)")

        # Show themes if available
        for theme in topic.get("themes", [])[:3]:
            theme_text = escape(theme.get("theme", ""))
            lines.append(f"  \U0001f4a1 {theme_text}")

        # Show summary
        summary = topic.get("summary", "")
        if summary:
            lines.append(f"  {escape(summary[:200])}{'…' if len(summary) > 200 else ''}")

        # Show top papers
        for paper in topic.get("top_papers", [])[:3]:
            raw_title = paper.get("title", "") or ""
            title = escape(raw_title[:80]) + ("…" if len(raw_title) > 80 else "")
            url = paper.get("url", "")
            conf = confidence_badge(paper.get("confidence")) if paper.get("confidence") else ""
            if url:
                lines.append(f'  \u2022 <a href="{safe_url(url)}">{title}</a> {conf}')
            else:
                lines.append(f"  \u2022 {title} {conf}")

    result = "\n".join(lines)
    return truncate(result)


def format_author_alert(author_name: str, papers: list[dict]) -> str:
    """Format an author alert notification for Telegram.

    Parameters
    ----------
    author_name : str
        Name of the tracked author.
    papers : list[dict]
        List of new papers by this author.

    Returns
    -------
    str
        HTML-formatted author alert message.
    """
    lines = [f"\U0001f514 <b>New papers by {escape(author_name)}</b>\n"]

    for paper in papers[:10]:
        lines.append(format_paper_card(paper))
        lines.append("")

    if len(papers) > 10:
        lines.append(f"<i>... and {len(papers) - 10} more</i>")

    return truncate("\n".join(lines))


def format_help() -> str:
    """Format the static /help command response listing all bot commands.

    Returns
    -------
    str
        HTML-formatted help text for Telegram.
    """
    lines = ["<b>JARVIS RD Assistant</b>"]
    current_group = ""
    for spec in COMMAND_CATALOG:
        if spec.group != current_group:
            current_group = spec.group
            lines.extend(["", f"<b>{html.escape(current_group)}:</b>"])
        usage = html.escape(spec.usage)
        lines.append(f"/{usage} — {html.escape(spec.description)}")
    return "\n".join(lines)
