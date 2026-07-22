"""Traversal-safe paths and hardened reads of small JSON state files."""

import json
import os
import stat
from pathlib import Path
from typing import Any

_MAX_STATE_FILE_BYTES = 64 * 1024


def secure_path(base: str | os.PathLike, *parts: str) -> Path:
    """Join path components without allowing traversal outside a base directory.

    Uses os.path.realpath so symlinks cannot escape the base directory.

    Parameters
    ----------
    base : str or os.PathLike
        Trusted directory that must contain the resolved result.
    *parts : str
        Untrusted or model-derived path components to join below ``base``.

    Returns
    -------
    pathlib.Path
        Fully resolved path contained by ``base``.

    Raises
    ------
    ValueError
        If the resolved path is outside ``base``.
    """
    base_real = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base_real, *parts))
    if full != base_real and not full.startswith(base_real + os.sep):
        raise ValueError(f"path escapes base directory: {parts!r}")
    return Path(full)


def read_regular_json_file(
    path: str | os.PathLike,
    *,
    max_bytes: int = _MAX_STATE_FILE_BYTES,
) -> Any:
    """Read bounded JSON from one singly linked regular file.

    ``FileNotFoundError`` means the path is absent. Other ``OSError`` instances
    and ``ValueError`` mean existing state is not trustworthy. Callers validate
    the schema because restore-token and quarantine records differ.

    The descriptor checks matter in addition to ``Path.is_file()``: ``O_NOFOLLOW``
    rejects symlinks, ``fstat`` rejects non-regular files and multiply-linked
    inodes, and the bounded read prevents an oversized state file from consuming
    unbounded memory. The surrounding directory is a trusted application volume.

    Parameters
    ----------
    path : str or os.PathLike
        Exact application state file to open.
    max_bytes : int
        Maximum accepted serialized size, including all JSON whitespace.

    Returns
    -------
    Any
        JSON-decoded value. Callers must validate their own exact schema.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    OSError
        If the file cannot be opened or read without following a link.
    ValueError
        If the file is not singly linked and regular, exceeds ``max_bytes``,
        contains invalid JSON, or ``max_bytes`` is not positive.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise ValueError("state path is not a singly linked regular file")
        raw = os.read(fd, max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("state file exceeds the size limit")
        return json.loads(raw)
    finally:
        os.close(fd)
