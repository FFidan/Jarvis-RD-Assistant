"""Tests for strip_think_blocks nested-tag handling (audit finding F-01).

Covers: nested tags, unterminated blocks, empty blocks, idempotence, no-residual
property, and parity with the streaming variant for non-nested inputs.
"""

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings
from jarvis_common.llm_client import strip_think_blocks, strip_think_streaming

# ---------------------------------------------------------------------------
# Concrete cases
# ---------------------------------------------------------------------------


def test_strip_think_strips_nested_blocks():
    """Nested think blocks must be fully stripped, not just the innermost pair."""
    raw = "<think>outer<think>inner</think>still</think>answer"
    assert strip_think_blocks(raw) == "answer"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Unterminated at end of string — all content from <think> onward discarded.
        ("<think>unterminated", ""),
        # Visible text before the unterminated block is preserved.
        ("text<think>unterminated", "text"),
        # Completed block followed by unterminated — completed block stripped, unterminated discarded.
        ("<think>first</think>visible<think>unclosed", "visible"),
    ],
)
def test_strip_think_strips_unterminated(raw, expected):
    """Unterminated <think> blocks are treated as infinite: content is discarded."""
    assert strip_think_blocks(raw) == expected


def test_strip_think_handles_empty_block():
    """An empty <think></think> pair is removed without affecting surrounding text."""
    assert strip_think_blocks("text<think></think>more") == "textmore"


def test_strip_think_handles_multiple_blocks():
    """Multiple non-nested think blocks are all stripped."""
    raw = "<think>a</think>first<think>b</think>second"
    assert strip_think_blocks(raw) == "firstsecond"


def test_strip_think_no_tags():
    """Input with no think tags is returned unchanged (modulo strip)."""
    assert strip_think_blocks("plain text") == "plain text"


def test_strip_think_empty_input():
    """Empty input returns empty string."""
    assert strip_think_blocks("") == ""


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------


@given(st.text())
@settings(max_examples=500)
def test_strip_think_idempotent(s: str):
    """Applying strip_think_blocks twice must equal applying it once."""
    once = strip_think_blocks(s)
    twice = strip_think_blocks(once)
    assert once == twice, f"Not idempotent: once={once!r}, twice={twice!r}"


@given(st.text())
@settings(max_examples=500)
def test_strip_think_no_residual_tags(s: str):
    """Output of strip_think_blocks must never contain a literal <think> substring."""
    result = strip_think_blocks(s)
    assert "<think>" not in result, f"Residual <think> found in: {result!r} (input: {s!r})"


# ---------------------------------------------------------------------------
# Parity with strip_think_streaming (non-nested inputs only)
#
# strip_think_streaming uses a binary in_think flag (not a depth counter), so it
# cannot handle nested tags identically.  Parity is asserted for inputs that do
# NOT contain nested <think> tags — i.e., inputs where the two implementations
# are specification-equivalent.
# ---------------------------------------------------------------------------

_PARITY_CORPUS = [
    # Simple single block
    "<think>hidden</think>answer",
    # Block at start
    "<think>cot</think>visible",
    # Block at end
    "prefix<think>trailing</think>",
    # No block
    "plain text",
    # Empty string
    "",
    # Empty block
    "a<think></think>b",
    # Multiple non-nested blocks
    "<think>a</think>mid<think>b</think>end",
    # Unterminated (agrees: both discard from <think> onward)
    "text<think>unterminated",
    # Multi-line block
    "<think>line1\nline2</think>after",
    # Whitespace only outside block
    "  <think>x</think>  ",
]


def _streaming_flush(text: str) -> str:
    """Feed text as a single chunk through strip_think_streaming and flush carry."""
    out, state, carry = strip_think_streaming(text, False, "")
    # Flush carry only if the stream ended outside a think block.
    tail = carry if not state else ""
    return (out + tail).strip()


@pytest.mark.parametrize("text", _PARITY_CORPUS)
def test_strip_think_parity_with_streaming(text: str):
    """strip_think_blocks and strip_think_streaming must agree on non-nested inputs."""
    blocks_result = strip_think_blocks(text)
    streaming_result = _streaming_flush(text)
    assert blocks_result == streaming_result, (
        f"Parity failure for {text!r}:\n"
        f"  strip_think_blocks   = {blocks_result!r}\n"
        f"  strip_think_streaming = {streaming_result!r}"
    )
