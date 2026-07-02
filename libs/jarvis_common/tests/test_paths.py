"""Unit tests for the traversal-safe secure_path helper."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from jarvis_common.paths import secure_path


def test_join_inside_base_returns_path(tmp_path: Path) -> None:
    result = secure_path(tmp_path, "sub", "file.txt")
    assert result == tmp_path / "sub" / "file.txt"


def test_no_parts_returns_base_itself(tmp_path: Path) -> None:
    assert secure_path(tmp_path) == tmp_path


def test_dotdot_escape_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes base directory"):
        secure_path(tmp_path, "..", "secret.txt")


def test_absolute_part_escaping_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes base directory"):
        secure_path(tmp_path, "/etc/passwd")


def test_sibling_prefix_escape_raises(tmp_path: Path) -> None:
    """`/base-evil` must not be treated as inside `/base` (suffix-prefix hole)."""
    base = tmp_path / "base"
    base.mkdir()
    (tmp_path / "base-evil").mkdir()
    with pytest.raises(ValueError, match="escapes base directory"):
        secure_path(base, "..", "base-evil", "loot.txt")


def test_symlink_pointing_outside_base_raises(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("secret")
    (base / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes base directory"):
        secure_path(base, "link", "loot.txt")


def test_symlink_pointing_inside_base_is_allowed(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "real").mkdir(parents=True)
    (base / "link").symlink_to(base / "real")
    result = secure_path(base, "link", "file.txt")
    assert str(result).startswith(os.path.realpath(base) + os.sep)
