"""Prompt-safety helpers for LLM input escaping and delimiter wrapping.

Provides a small, focused set of functions that prevent prompt injection
via angle-bracket tag forgery in LLM prompts.  All LLM call sites that
interpolate untrusted user or paper text should use these helpers.

Out of scope: ``${...}``, ``{{...}}``, backtick blocks, Jinja templates.
The ``escape`` and ``strip`` modes both flatten BIDI/zero-width chars but
do not recognise template syntax.  If a templating engine is introduced
into LLM prompts, add a ``mode='template'`` branch to :func:`safe_for_prompt`.
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


# Valid XML tag names for wrap_delimited: start with letter or underscore,
# followed by letters, digits, or underscores only.
_TAG_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _strip_bidi_zw(text: str) -> str:
    """Remove BIDI override/isolate and zero-width characters from *text*.

    Only strips the targeted Unicode ranges; CJK, emoji, accented characters,
    regular spaces, newlines, and tabs are fully preserved.
    """
    return _BIDI_ZW_RE.sub("", text)


def safe_for_prompt(text: str | None, mode: str = "escape") -> str:
    """Sanitise text before interpolating it into an LLM prompt.

    .. warning::
        ``mode='strip'`` removes control / BIDI / zero-width characters but
        does **NOT** escape ``<`` and ``>``. If the sanitised text will be
        interpolated into a prompt that uses XML-style delimiters (e.g.
        ``<paper_text>...</paper_text>``), use ``mode='escape'`` (the
        default) or :func:`wrap_delimited`. Otherwise an attacker can close
        the surrounding tag with ``</paper_text>`` and inject instructions
        the LLM treats as part of the system prompt.

    Parameters
    ----------
    text:
        Raw input string (user question, paper title, abstract, etc.).
        ``None`` is treated as an empty string.
    mode:
        ``'escape'`` *(default, recommended for prompt interpolation)* —
        HTML-encode ``<`` and ``>`` so XML-style delimiters cannot be forged
        (former :func:`escape_llm_text` behaviour).

        ``'delimit'`` — reserved sentinel; **always raises**
        ``ValueError``. There is no valid caller (``wrap_delimited``
        uses ``mode='escape'`` internally). Call :func:`wrap_delimited`
        directly to escape-and-wrap text in XML delimiters.

        ``'strip'`` — strip ASCII/Unicode control characters that could
        confuse tokenisers or embed hidden instructions. **Does not escape
        angle brackets** — only safe for text that will NOT be wrapped in
        XML-style delimiters in the LLM prompt. Prefer ``mode='escape'``
        (or :func:`wrap_delimited`) for any prompt-interpolation use case.

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
    # _CTRL_RE removes BIDI + zero-width chars in both escape and strip modes;
    # no separate _strip_bidi_zw pass needed.
    if mode == "escape":
        return _CTRL_RE.sub("", text).replace("<", "&lt;").replace(">", "&gt;")
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


def wrap_delimited(tag: str, text: str, *, max_chars: int | None = None) -> tuple[str, bool]:
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
        Must match ``[a-zA-Z_][a-zA-Z0-9_]*`` to prevent delimiter injection.
    text:
        Raw untrusted input string.
    max_chars:
        If given, truncate *after* escaping to this many characters.

    Returns
    -------
    tuple[str, bool]
        A ``(delimited_text, truncated)`` pair.  *truncated* is ``True`` when
        *max_chars* was set and the escaped body exceeded that limit.

    Raises
    ------
    ValueError
        If *tag* contains characters outside ``[a-zA-Z_][a-zA-Z0-9_]*``.
    """
    if not _TAG_RE.match(tag):
        raise ValueError(f"Invalid tag {tag!r}: must match [a-zA-Z_][a-zA-Z0-9_]*")
    body = escape_llm_text(_strip_bidi_zw(text))
    truncated = False
    if max_chars is not None and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    return f"<{tag}>\n{body}\n</{tag}>", truncated
