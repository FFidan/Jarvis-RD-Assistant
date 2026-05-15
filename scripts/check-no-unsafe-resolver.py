#!/usr/bin/env python3
"""WS-AUTH guard: forbid the permissive user-id resolvers in router files.

``current_user_id`` / ``current_user_id_or_none`` resolve to ``None`` for
API-key-only callers. On a user-data route that lets an ops credential fall
through as a permissionless shared user. After WS-AUTH such routes must use
``current_user_id_strict`` (hard 401) instead.

This linter walks ``services/*/*/routers/*.py`` and flags any file that
*imports* or *uses in a ``Depends(...)``* one of the unsafe names, unless the
file is in :data:`ALLOWLIST` (genuinely ops/public routes). It is expected to
fail loudly until WS-CROSS-USER migrates the remaining routers.

Pure stdlib (``ast`` + ``pathlib``); exits non-zero on any violation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Forbidden resolver names (the permissive, None-returning ones).
UNSAFE_NAMES = frozenset({"current_user_id", "current_user_id_or_none"})

# Router files that legitimately serve ops/public traffic (no per-user data
# to protect) and may keep the permissive resolvers. Paths are relative to the
# repo root. Be conservative — when unsure, do NOT allowlist; WS-CROSS-USER
# will migrate the rest.
ALLOWLIST = frozenset(
    {
        "services/paper_ingestion/paper_ingestion/routers/system.py",
        "services/paper_ingestion/paper_ingestion/routers/logs.py",
        "services/paper_ingestion/paper_ingestion/routers/telegram.py",
        "services/paper_ingestion/paper_ingestion/routers/infra_events.py",
        "services/paper_ingestion/paper_ingestion/routers/setup.py",
    }
)


def _imports_unsafe(tree: ast.Module) -> tuple[list[tuple[int, str]], frozenset[str]]:
    """Return import violations and the set of local alias names for unsafe resolvers.

    The second return value collects every local name that aliases an unsafe
    resolver (e.g. ``from jarvis_common.auth import current_user_id_or_none as uid``
    yields ``"uid"``).  The caller passes this set to :func:`_depends_on_unsafe`
    so that ``Depends(uid)`` is caught even though ``"uid"`` is not in
    :data:`UNSAFE_NAMES`.
    """
    hits: list[tuple[int, str]] = []
    unsafe_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in UNSAFE_NAMES:
                    hits.append((node.lineno, f"import {alias.name}"))
                    # Track local alias (or the original name if no alias).
                    local_name = alias.asname if alias.asname else alias.name
                    unsafe_aliases.add(local_name)
    return hits, frozenset(unsafe_aliases)


def _depends_on_unsafe(
    tree: ast.Module,
    unsafe_aliases: frozenset[str] = frozenset(),
) -> list[tuple[int, str]]:
    """Return (lineno, name) for every ``Depends(<unsafe>)`` call.

    Flags:
    * ``Depends(current_user_id_or_none)`` — bare name in :data:`UNSAFE_NAMES`
    * ``Depends(uid)`` — local alias discovered by :func:`_imports_unsafe`
    * ``Depends(auth.current_user_id_or_none)`` — attribute-form access
    """
    all_unsafe_names = UNSAFE_NAMES | unsafe_aliases
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Depends" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name) and arg.id in all_unsafe_names:
            hits.append((node.lineno, f"Depends({arg.id})"))
        elif isinstance(arg, ast.Attribute) and arg.attr in UNSAFE_NAMES:
            # e.g. Depends(auth.current_user_id_or_none)
            hits.append((node.lineno, f"Depends(…{arg.attr})"))
    return hits


def main() -> int:
    routers = sorted(_REPO_ROOT.glob("services/*/*/routers/*.py"))
    failed = False
    for path in routers:
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            print(f"{rel}: could not parse ({exc})", file=sys.stderr)
            failed = True
            continue
        import_hits, unsafe_aliases = _imports_unsafe(tree)
        violations = import_hits + _depends_on_unsafe(tree, unsafe_aliases)
        if violations:
            failed = True
            print(f"{rel}: uses permissive user-id resolver (use current_user_id_strict):")
            for lineno, what in sorted(violations):
                print(f"  {rel}:{lineno}: {what}")

    if failed:
        print(
            "\nWS-AUTH: the files above must migrate to current_user_id_strict "
            "or be added to ALLOWLIST if genuinely ops/public.",
            file=sys.stderr,
        )
        return 1
    print("check-no-unsafe-resolver: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
