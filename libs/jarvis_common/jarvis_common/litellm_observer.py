"""Track which model LiteLLM actually served per request alias."""

from __future__ import annotations

from collections import Counter, deque
from threading import Lock

_LOCK = Lock()
_RECENT: deque[tuple[str, str]] = deque(maxlen=50)  # (alias, served_model)


def record_serve(alias: str, served_model: str) -> None:
    """Append an (alias, served_model) observation to the rolling window."""
    with _LOCK:
        _RECENT.append((alias, served_model))


def observed_share(alias: str) -> tuple[str | None, float]:
    """Return (most-common-served-model-for-alias, share-of-last-N).

    Parameters
    ----------
    alias:
        The LiteLLM model alias to query (e.g. ``"smart"``).

    Returns
    -------
    tuple[str | None, float]
        ``(served_model, share)`` where *share* is in ``[0.0, 1.0]``.
        Returns ``(None, 0.0)`` when no observations exist for *alias*.

    """
    with _LOCK:
        items = [m for a, m in _RECENT if a == alias]
    if not items:
        return None, 0.0
    top, count = Counter(items).most_common(1)[0]
    return top, count / len(items)
