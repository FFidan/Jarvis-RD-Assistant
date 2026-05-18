#!/usr/bin/env python3
"""AST-based linter for JSONB double-encoding.

Flags Python source where a call like ``await conn.execute(...)`` (or
``executemany``, ``fetch``, ``fetchrow``, ``fetchval``) passes an arg that is a
``json.dumps(...)`` call AND the SQL string literal contains ``::jsonb``.
Catches multi-line patterns the previous grep-window-based linter missed.

Supports ``# nolint:jsonb-double-encode`` on the call's line to suppress.

Exit code: 0 = clean, 1 = violations found, 2 = usage error.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

JSONB_HINT = re.compile(r"::jsonb", re.IGNORECASE)
EXEC_METHODS = {"execute", "executemany", "fetch", "fetchrow", "fetchval"}


def _is_json_dumps(node: ast.expr) -> bool:
    """Return True if *node* is a ``json.dumps(...)`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # json.dumps(...)
    if isinstance(func, ast.Attribute):
        if func.attr == "dumps" and isinstance(func.value, ast.Name) and func.value.id == "json":
            return True
    # Could also be imported as `from json import dumps` → bare Name "dumps"
    # We only catch the qualified form (json.dumps) since bare `dumps` is ambiguous.
    return False


def _sql_contains_jsonb(node: ast.expr) -> bool:
    """Return True if *node* is a string constant containing ``::jsonb``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(JSONB_HINT.search(node.value))
    # f-strings (ast.JoinedStr) — check sub-values
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(v, ast.Constant) and isinstance(v.value, str) and JSONB_HINT.search(v.value)
            for v in ast.walk(node)
        )
    return False


def _all_args(call: ast.Call) -> list[ast.expr]:
    """Return all positional and keyword-value args of a Call node."""
    return list(call.args) + [kw.value for kw in call.keywords]


def _is_db_exec_call(node: ast.expr) -> bool:
    """Return True if *node* is a method call whose name is in EXEC_METHODS."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in EXEC_METHODS


def check_file(path: Path) -> list[tuple[int, str]]:
    """Return a list of (line_no, message) violations in *path*."""
    source = path.read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # Unwrap ``await expr`` → examine the inner Call
        inner: ast.expr
        if isinstance(node, ast.Await):
            inner = node.value
        else:
            inner = node  # type: ignore[assignment]

        if not _is_db_exec_call(inner):
            continue

        call: ast.Call = inner  # type: ignore[assignment]
        all_args = _all_args(call)

        has_jsonb_sql = any(_sql_contains_jsonb(a) for a in all_args)
        has_json_dumps = any(_is_json_dumps(a) for a in all_args)

        if not (has_jsonb_sql and has_json_dumps):
            continue

        # Determine line number (use the Call node's lineno)
        lineno: int = getattr(call, "lineno", 0)

        # Check for suppression comment on the call's line (1-indexed)
        if 0 < lineno <= len(lines):
            if "nolint:jsonb-double-encode" in lines[lineno - 1]:
                continue

        violations.append(
            (
                lineno,
                f"json.dumps() arg passed to {getattr(call.func, 'attr', '?')}() "
                f"alongside ::jsonb — likely double-encode (asyncpg codec auto-encodes JSONB)",
            )
        )

    return violations


def main(argv: list[str] | None = None) -> int:
    """Scan path roots for JSONB double-encode violations and report them.

    Parameters
    ----------
    argv : list[str] or None
        Paths to scan. When ``None``, defaults to ``sys.argv[1:]``. When no
        paths are provided at all, scans ``services/`` and ``libs/`` relative
        to the repo root.

    Returns
    -------
    int
        0 if no violations are found, 1 if violations exist, 2 on usage error.
    """
    roots = argv if argv is not None else sys.argv[1:]
    if not roots:
        # Default: walk services/ and libs/ relative to this script's repo root
        script_dir = Path(__file__).resolve().parent
        repo_root = script_dir.parent
        roots = [str(repo_root / "services"), str(repo_root / "libs")]

    all_violations: list[tuple[Path, int, str]] = []

    for root_str in roots:
        root = Path(root_str)
        if not root.exists():
            print(f"WARNING: path does not exist, skipping: {root}", file=sys.stderr)
            continue
        for py_file in sorted(root.rglob("*.py")):
            for lineno, msg in check_file(py_file):
                all_violations.append((py_file, lineno, msg))

    if all_violations:
        print("ERROR: JSONB double-encode violations found:\n")
        for file_path, lineno, msg in all_violations:
            print(f"  {file_path}:{lineno}: {msg}")
        print(
            "\nasyncpg's JSONB codec (init_pg_connection) auto-encodes via json.dumps."
            "\nPass native dict/list/value directly to $N::jsonb."
            "\nTo suppress: add  # nolint:jsonb-double-encode  on the execute() line."
        )
        return 1

    print("OK: no jsonb double-encode patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
