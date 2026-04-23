"""Docker Secrets / _FILE convention helper.

Reads a named secret preferring the ``<NAME>_FILE`` environment variable
(Docker Secrets mount path) over the plain ``<NAME>`` environment variable.

Usage::

    from jarvis_common.secrets import read_secret

    api_key = read_secret("JARVIS_API_KEY")          # checks JARVIS_API_KEY_FILE first
    token   = read_secret("TELEGRAM_BOT_TOKEN")      # checks TELEGRAM_BOT_TOKEN_FILE first
"""

import os
from pathlib import Path


def read_secret(name: str) -> str:
    """Return the value for *name*, honouring the ``_FILE`` convention.

    Resolution order:

    1. ``{name}_FILE`` env var — read and strip the file at that path.
    2. ``{name}`` env var — return as-is.
    3. Empty string if neither is set.

    Parameters
    ----------
    name:
        The environment variable name (e.g. ``"JARVIS_API_KEY"``).

    Returns
    -------
    str
        The secret value, or ``""`` if not configured.
    """
    file_var = os.environ.get(f"{name}_FILE", "")
    if file_var:
        try:
            value = Path(file_var).read_text().strip()
        except OSError as exc:
            raise RuntimeError(f"Failed to read secret from {file_var!r}") from exc
        return value
    return os.environ.get(name, "")
