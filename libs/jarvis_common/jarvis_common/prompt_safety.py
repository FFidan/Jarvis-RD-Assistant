"""Prompt-safety helpers for LLM input escaping and delimiter wrapping.

Provides a small, focused set of functions that prevent prompt injection
via angle-bracket tag forgery in LLM prompts.  All LLM call sites that
interpolate untrusted user or paper text should use these helpers.
"""

from __future__ import annotations


def escape_llm_text(text: str) -> str:
    """Escape angle-brackets so tagged delimiters can't be forged by input.

    Replaces ``<`` with ``&lt;`` and ``>`` with ``&gt;`` so that crafted
    inputs like ``</paper_text>IGNORE ABOVE`` cannot break out of their
    delimiter tags in the LLM prompt.

    Parameters
    ----------
    text:
        Raw input string (user question, paper title, abstract, etc.).

    Returns
    -------
    str
        Escaped string safe for interpolation inside XML-style delimiters.
    """
    return text.replace("<", "&lt;").replace(">", "&gt;")


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
    body = escape_llm_text(text)
    if max_chars is not None and len(body) > max_chars:
        body = body[:max_chars]
    return f"<{tag}>\n{body}\n</{tag}>"
