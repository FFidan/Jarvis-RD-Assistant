"""Grouping of ordered text chunks into char-budget windows."""

from __future__ import annotations

import re
from collections.abc import Sequence

_HEADING_RE = re.compile(r"^##\s")


def _window_len(items: list[str]) -> int:
    return sum(len(t) for t in items) + max(len(items) - 1, 0)


def chunk_windows(chunks: Sequence[str], max_chars: int) -> list[list[str]]:
    """Group ordered chunk texts into consecutive windows of at most ``max_chars``.

    Every chunk lands in exactly one window, in input order — nothing is
    dropped or reordered.  A window's size is the sum of its chunk lengths
    plus one joiner character between consecutive chunks (callers join window
    members with ``"\\n"``).

    Windows prefer to break at section boundaries: a chunk starting with
    ``"## "`` (the only structural marker the text splitter emits) marks a
    new section.  When a window overflows mid-section, the chunks belonging
    to the most recently started section move to the next window so sections
    stay whole when avoidable.  Chunk sequences without headings window by
    budget alone.  A single chunk longer than ``max_chars`` forms its own
    window rather than being split or dropped.

    Parameters
    ----------
    chunks:
        Chunk texts in ascending chunk-index order.
    max_chars:
        Character budget per window (must be positive).
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive, got {max_chars}")

    windows: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for text in chunks:
        extra = len(text) + (1 if current else 0)
        if current and current_len + extra > max_chars:
            carry: list[str] = []
            if not _HEADING_RE.match(text):
                heading_at = next(
                    (i for i in range(len(current) - 1, 0, -1) if _HEADING_RE.match(current[i])),
                    0,
                )
                if heading_at > 0:
                    carry = current[heading_at:]
                    current = current[:heading_at]
            windows.append(current)
            current = carry
            current_len = _window_len(carry)
            extra = len(text) + (1 if current else 0)
            if current and current_len + extra > max_chars:
                windows.append(current)
                current = []
                current_len = 0
                extra = len(text)
        current.append(text)
        current_len += extra

    if current:
        windows.append(current)
    return windows
