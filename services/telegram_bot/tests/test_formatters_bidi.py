"""Tests for TG-007: BIDI control and zero-width character stripping in escape()."""

from telegram_bot.formatters import escape


def test_escape_strips_bidi_rtl_override():
    """BIDI right-to-left override (U+202E) is stripped before HTML escaping."""
    # U+202E is the RIGHT-TO-LEFT OVERRIDE character (‮)
    result = escape("hello‮world")
    assert result == "helloworld"


def test_escape_strips_bidi_ltr_embedding():
    """BIDI left-to-right embedding (U+202A) is stripped."""
    result = escape("hello‪world")
    assert result == "helloworld"


def test_escape_strips_zero_width_space():
    """Zero-width space (U+200B) is stripped."""
    result = escape("hello​world")
    assert result == "helloworld"


def test_escape_strips_zero_width_joiner():
    """Zero-width joiner (U+200D) is stripped."""
    result = escape("hello‍world")
    assert result == "helloworld"


def test_escape_strips_bom():
    """BOM / zero-width no-break space (U+FEFF) is stripped."""
    result = escape("﻿hello")
    assert result == "hello"


def test_escape_html_chars_still_escaped():
    """HTML special characters are still escaped after BIDI stripping."""
    result = escape('<script>alert("xss")</script>')
    assert "&lt;" in result
    assert "&gt;" in result
    assert "&quot;" in result
    assert "<script>" not in result


def test_escape_combined_bidi_and_html():
    """BIDI chars are stripped AND HTML is escaped in the same string."""
    result = escape("<b>‮hello</b>")
    assert "‮" not in result
    assert "&lt;" in result
    assert "<b>" not in result


def test_escape_none_returns_empty_string():
    """None input returns empty string without raising."""
    result = escape(None)
    assert result == ""


def test_escape_normal_text_unchanged():
    """Normal ASCII text with no BIDI or HTML special chars is returned as-is."""
    result = escape("Hello, World!")
    assert result == "Hello, World!"


def test_escape_bidi_string_from_spec():
    """Spec example: escape('hello‮world') returns 'helloworld'."""
    assert escape("hello‮world") == "helloworld"
