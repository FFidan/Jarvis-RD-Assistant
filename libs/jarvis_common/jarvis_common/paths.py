"""Traversal-safe path joining for request- or model-derived path parts."""

import os
from pathlib import Path


def secure_path(base: str | os.PathLike, *parts: str) -> Path:
    """Join *parts* under *base*; raise ValueError if the result escapes base.

    Uses os.path.realpath so symlinks cannot escape the base directory.
    """
    base_real = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base_real, *parts))
    if full != base_real and not full.startswith(base_real + os.sep):
        raise ValueError(f"path escapes base directory: {parts!r}")
    return Path(full)
