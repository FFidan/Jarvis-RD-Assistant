"""Tests for TG-007: BIDI control and zero-width character stripping in escape()."""

import pytest
from telegram_bot.formatters import escape


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello\u202eworld", "helloworld"),
        ("hello\u202aworld", "helloworld"),
        ("hello\u200bworld", "helloworld"),
        ("hello\u200dworld", "helloworld"),
        ("\ufeffhello", "hello"),
        (None, ""),
        ("Hello, World!", "Hello, World!"),
        ("hello\u202eworld", "helloworld"),
        ("hello\u200eworld", "helloworld"),
        ("hello\u200fworld", "helloworld"),
        ("\u200ehello\u200fworld\u200e", "helloworld"),
    ],
    ids=(
        "right-to-left-override",
        "left-to-right-embedding",
        "zero-width-space",
        "zero-width-joiner",
        "byte-order-mark",
        "none",
        "plain-text",
        "spec-example",
        "left-to-right-mark",
        "right-to-left-mark",
        "combined-directional-marks",
    ),
)
def test_escape_strips_invisible_controls(raw: str | None, expected: str) -> None:
    """Escape handles neutral inputs and removes invisible directional controls."""
    assert escape(raw) == expected


def test_escape_html_chars_still_escaped() -> None:
    """HTML special characters are still escaped after BIDI stripping."""
    result = escape('<script>alert("xss")</script>')
    assert "&lt;" in result
    assert "&gt;" in result
    assert "&quot;" in result
    assert "<script>" not in result


def test_escape_combined_bidi_and_html() -> None:
    """BIDI chars are stripped AND HTML is escaped in the same string."""
    result = escape("<b>\u202ehello</b>")
    assert "\u202e" not in result
    assert "&lt;" in result
    assert "<b>" not in result
