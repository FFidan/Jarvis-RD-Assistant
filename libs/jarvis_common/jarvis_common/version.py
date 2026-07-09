"""Canonical application version helper.

Single source of truth for the installed ``jarvis-rd-assistant`` distribution
version. Surfaced by ``/health/internal`` (:mod:`jarvis_common.health`) and by
each service's ``FastAPI(version=...)`` app metadata — both call
:func:`app_version` rather than re-deriving it, so the two never drift.
"""

from __future__ import annotations

import importlib.metadata
import os


def app_version() -> str:
    """The running application version.

    Resolution order:
    1. the installed ``jarvis-rd-assistant`` distribution metadata (present only
       when the root package is pip-installed);
    2. the ``JARVIS_VERSION`` environment variable — the deployment's single
       source of truth (docker-compose defaults it to the release tag and uses
       it for the image tags and the backup manifest). The service images install
       only the ``jarvis_common`` wheel plus copied source, so the root
       distribution is *not* discoverable in-container and this is the effective
       production path;
    3. ``"unknown"`` when neither is available (a bare source checkout).
    """
    try:
        return importlib.metadata.version("jarvis-rd-assistant")
    except importlib.metadata.PackageNotFoundError:
        return os.environ.get("JARVIS_VERSION") or "unknown"
