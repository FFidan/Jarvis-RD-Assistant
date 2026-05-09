"""Tests for shared Server-Sent Event frame helpers."""

from __future__ import annotations

from jarvis_common.sse import SSE_DONE, sse_event, sse_keepalive


def test_sse_event_formats_payload_as_single_data_frame() -> None:
    """Payloads should be JSON-encoded behind one ``data:`` prefix."""
    assert sse_event({"status": "ok", "count": 2}) == 'data: {"status": "ok", "count": 2}\n\n'


def test_sse_done_constant_matches_protocol_sentinel() -> None:
    """The done sentinel should stay compatible with existing SSE clients."""
    assert SSE_DONE == "data: [DONE]\n\n"


def test_sse_keepalive_returns_comment_frame() -> None:
    """Keepalive frames should use SSE comment syntax and a blank terminator."""
    assert sse_keepalive() == ": keepalive\n\n"
