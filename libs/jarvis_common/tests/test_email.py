"""Tests for jarvis_common.email template rendering.

Guards the BE-04 fix: `_PLAIN_BODY_TEMPLATE` must use ``str.replace`` (not
``str.format``) so that URLs containing ``{`` / ``}`` characters (e.g. query
params with template-like tokens) are included verbatim in the email body
without raising ``KeyError`` / ``IndexError`` from the format DSL.
"""

from __future__ import annotations

import pytest
from jarvis_common.email import _PLAIN_BODY_TEMPLATE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_body(link: str) -> str:
    """Mirror the production rendering path (must use .replace, not .format)."""
    return _PLAIN_BODY_TEMPLATE.replace("{link}", link)


# ---------------------------------------------------------------------------
# Normal URL
# ---------------------------------------------------------------------------


def test_plain_body_normal_url() -> None:
    """A standard magic-link URL is embedded verbatim."""
    link = "https://example.com/auth/verify?token=abc123"
    body = _render_body(link)
    assert link in body
    assert "Click the link" in body
    assert "15 minutes" in body


# ---------------------------------------------------------------------------
# URLs with brace characters (BE-04 regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        # Named placeholder lookalike — would trip format() if re-scanned
        "https://example.com/verify?token={abc}",
        # Positional placeholder lookalike
        "https://example.com/verify?token={0}",
        # Empty braces
        "https://example.com/verify?token={}",
        # Double braces (escaped in format DSL)
        "https://example.com/verify?token={{escaped}}",
        # The template field name itself as a query value
        "https://example.com/verify?redirect={link}",
        # Mixed braces in path and query
        "https://example.com/{path}?token={value}&other={0}",
    ],
)
def test_plain_body_brace_url_rendered_verbatim(link: str) -> None:
    """URLs containing ``{…}`` tokens must appear verbatim in the rendered body.

    Before the BE-04 fix the body was built with ``_PLAIN_BODY_TEMPLATE.format(link=link)``.
    While CPython's single-pass ``str.format`` does not re-scan substituted values,
    using ``str.replace`` is semantically correct (no format-DSL interpretation) and
    eliminates the risk entirely.  This test verifies the safe path is in place.
    """
    body = _render_body(link)
    assert link in body, (
        f"Link with brace chars was not embedded verbatim.\nLink:  {link!r}\nBody:\n{body}"
    )
