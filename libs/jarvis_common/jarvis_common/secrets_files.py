"""Shared Docker-secret file reader with a primary-value fallback.

Services accept a secret either directly (a plain environment variable) or
through the ``*_FILE`` Docker-secret convention (a mounted file path). This
module centralises that resolution so every service reads the two sources
identically and fails safe: an unreadable secret file logs a warning and
resolves to ``None`` so callers treat the secret as unset instead of aborting.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["read_secret_with_file_fallback"]


def read_secret_with_file_fallback(direct: str | None, file_env: str) -> str | None:
    """Return a secret from its direct value or a Docker-secret file fallback.

    Parameters
    ----------
    direct : str or None
        The secret's primary value (typically a plain environment variable).
        Returned unchanged when truthy.
    file_env : str
        Filesystem path to a mounted secret file (typically sourced from a
        ``<NAME>_FILE`` environment variable). Read and stripped only when
        *direct* is falsy.

    Returns
    -------
    str or None
        *direct* when it is truthy; otherwise the stripped file contents, or
        ``None`` when *file_env* is empty, the file is empty/whitespace, or the
        file cannot be read.

    Notes
    -----
    An ``OSError`` while reading the file is logged at warning level and
    resolved to ``None`` (fail-safe): the caller treats the secret as unset
    rather than raising. The secret value itself is never logged.
    """
    if direct:
        return direct
    if not file_env:
        return None
    try:
        return Path(file_env).read_text().strip() or None
    except OSError as exc:
        logger.warning("Could not read secret file %r: %s", file_env, exc)
        return None
