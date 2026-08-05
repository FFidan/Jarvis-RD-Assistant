"""Tests for jarvis_common public API (__all__) hygiene.

Ensures private / internal symbols are not re-exported via the top-level
package, which would couple callers to implementation details, and that the
declared export surface stays limited to names something actually imports
from the top level.
"""

import ast
import os
from collections import Counter
from pathlib import Path

import jarvis_common

# tests/ -> jarvis_common/ (dist root) -> libs/ -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXCLUDED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}


def _count_top_level_imports(repo_root: Path) -> Counter[str]:
    """Count ``from jarvis_common import <name>`` statements across the repo.

    Asserts that the census can actually see its subject — the package
    directory exists, a plausible number of Python files was scanned, and at
    least one top-level import was found — so a broken or misdirected scan
    fails loudly instead of reporting an empty violation list.
    """
    package_dir = repo_root / "libs" / "jarvis_common" / "jarvis_common"
    assert package_dir.is_dir(), f"census input missing: {package_dir}"
    assert (repo_root / "services").is_dir(), f"census input missing: {repo_root / 'services'}"

    counts: Counter[str] = Counter()
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = Path(dirpath) / fname
            scanned += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module == "jarvis_common"
                ):
                    for alias in node.names:
                        counts[alias.name] += 1

    assert scanned > 100, f"census scanned only {scanned} Python files — wrong input tree"
    assert counts, "census found zero top-level jarvis_common imports — measurement is broken"
    return counts


def test_langfuse_lifespan_hook_not_in_all() -> None:
    """_langfuse_lifespan_hook is an internal hook; must not be in __all__.

    Policy: private symbols prefixed with '_' must not be
    advertised in the public API. app_factory imports it directly from
    jarvis_common.llm_client, not via the jarvis_common top-level package.
    """
    assert "_langfuse_lifespan_hook" not in jarvis_common.__all__


def test_all_names_have_top_level_importers() -> None:
    """Every name in __all__ has at least one top-level importer in the repo.

    Guards the export surface against re-growing: a name nobody imports via
    ``from jarvis_common import <name>`` belongs to its defining submodule
    only. The census is an AST scan of every Python file in the repository,
    so string mentions and submodule imports do not count.
    """
    counts = _count_top_level_imports(_REPO_ROOT)
    unimported = sorted(name for name in jarvis_common.__all__ if counts[name] == 0)
    assert not unimported, (
        "__all__ declares names with zero top-level importers "
        f"(import them from their submodule instead): {unimported}"
    )
