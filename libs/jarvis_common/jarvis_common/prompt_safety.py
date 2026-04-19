"""Prompt-safety helpers for LLM input escaping and delimiter wrapping.

Provides a small, focused set of functions that prevent prompt injection
via angle-bracket tag forgery in LLM prompts.  All LLM call sites that
interpolate untrusted user or paper text should use these helpers.
"""

from __future__ import annotations

import re

# Control characters to strip in 'strip' mode (C0, C1, and a few unicode specials).
_CTRL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)

# BIDI override + isolate characters and zero-width characters that can be used
# to confuse LLMs or bypass content filters.  Stripped unconditionally in
# wrap_delimited() regardless of 'mode'.
#   U+202A-202E: BIDI embedding/override (LRE, RLE, PDF, LRO, RLO)
#   U+2066-2069: BIDI isolate (LRI, RLI, FSI, PDI)
#   U+200B-200D: zero-width space, non-joiner, joiner
#   U+FEFF:      BOM / zero-width no-break space
_BIDI_ZW_RE = re.compile(
    "[\u202a-\u202e"  # BIDI embedding/override chars
    "\u2066-\u2069"  # BIDI isolate chars
    "\u200b-\u200d"  # zero-width space/non-joiner/joiner
    "\ufeff"  # BOM / zero-width no-break space
    "]"
)


def _strip_bidi_zw(text: str) -> str:
    """Remove BIDI override/isolate and zero-width characters from *text*.

    Only strips the targeted Unicode ranges; CJK, emoji, accented characters,
    regular spaces, newlines, and tabs are fully preserved.
    """
    return _BIDI_ZW_RE.sub("", text)


def safe_for_prompt(text: str | None, mode: str = "escape") -> str:
    """Sanitise text before interpolating it into an LLM prompt.

    Parameters
    ----------
    text:
        Raw input string (user question, paper title, abstract, etc.).
        ``None`` is treated as an empty string.
    mode:
        ``'escape'`` — HTML-encode ``<`` and ``>`` so XML-style delimiters
        cannot be forged (former :func:`escape_llm_text` behaviour).

        ``'delimit'`` — escape then wrap in XML delimiters.  Not useful
        on its own; delegates to :func:`wrap_delimited`.  Raises
        ``ValueError`` unless called via :func:`wrap_delimited`.

        ``'strip'`` — strip ASCII/Unicode control characters that could
        confuse tokenisers or embed hidden instructions.

    Returns
    -------
    str
        Sanitised string safe for use in LLM prompts.

    Raises
    ------
    ValueError
        If *mode* is not one of the recognised values.
    """
    if text is None:
        text = ""
    if mode == "escape":
        return text.replace("<", "&lt;").replace(">", "&gt;")
    if mode == "strip":
        return _CTRL_RE.sub("", text)
    if mode == "delimit":
        raise ValueError(
            "mode='delimit' must be used via wrap_delimited(), not safe_for_prompt() directly"
        )
    raise ValueError(f"Unknown safe_for_prompt mode: {mode!r}")


def escape_llm_text(text: str) -> str:
    """Escape angle-brackets so tagged delimiters can't be forged by input.

    Replaces ``<`` with ``&lt;`` and ``>`` with ``&gt;`` so that crafted
    inputs like ``</paper_text>IGNORE ABOVE`` cannot break out of their
    delimiter tags in the LLM prompt.

    .. deprecated::
        Use :func:`safe_for_prompt` with ``mode='escape'`` (the default).

    Parameters
    ----------
    text:
        Raw input string (user question, paper title, abstract, etc.).

    Returns
    -------
    str
        Escaped string safe for interpolation inside XML-style delimiters.
    """
    return safe_for_prompt(text, mode="escape")


def wrap_delimited(tag: str, text: str, *, max_chars: int | None = None) -> str:
    """Escape, optionally truncate, and wrap text in XML-style delimiters.

    Produces a string of the form::

        <tag>
        <escaped text>
        </tag>

    Useful for wrapping untrusted content (paper body, user question) so the
    LLM can clearly identify where data ends and instructions resume.

    Parameters
    ----------
    tag:
        The XML tag name to use (e.g. ``"paper_text"``, ``"user_question"``).
    text:
        Raw untrusted input string.
    max_chars:
        If given, truncate *after* escaping to this many characters.

    Returns
    -------
    str
        Delimited, escaped (and optionally truncated) string.
    """
    body = escape_llm_text(_strip_bidi_zw(text))
    if max_chars is not None and len(body) > max_chars:
        body = body[:max_chars]
    return f"<{tag}>\n{body}\n</{tag}>"
