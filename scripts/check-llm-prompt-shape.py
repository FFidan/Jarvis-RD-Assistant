#!/usr/bin/env python3
"""Enforce the LLM prompt-shape contract on every ``call_llm_structured`` callsite.

See ``docs/contracts/09-llm-prompt-shape.md`` for the full convention. In short:

* Shape A (split-role): pass ``options=ChatCompletionOptions(system=...)`` so the
  instruction head lives in a system-role message and the user-role ``prompt=``
  carries only data (typically wrapped via ``wrap_delimited``).
* Shape B (carve-out): annotate the callsite with the marker
  ``# llm-prompt-shape: SINGLE-USER`` and document the rationale in the enclosing
  function's docstring.

Anything else is a violation. Test files (``test_*.py`` or anything under a
``tests/`` directory) are skipped — tests routinely build minimal prompts and
are not a public-bound surface.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_CARVEOUT_MARKER = "# llm-prompt-shape: SINGLE-USER"
_TARGET_FUNC = "call_llm_structured"
_OPTIONS_BUILDER = "ChatCompletionOptions"
_DEFAULT_ROOTS = ("services", "libs")


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            parts = path.parts
            if any(p == "tests" for p in parts):
                continue
            if path.name.startswith("test_"):
                continue
            files.append(path)
    return files


def _import_aliases(tree: ast.Module) -> set[str]:
    """Return the set of local names that resolve to ``call_llm_structured``.

    Handles ``from jarvis_common.llm_client import call_llm_structured`` plus
    aliased imports. Bare ``llm_client.call_llm_structured`` is detected by
    attribute match in :func:`_callable_name`.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("llm_client"):
            for alias in node.names:
                if alias.name == _TARGET_FUNC:
                    names.add(alias.asname or alias.name)
    return names


def _callable_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_system_kw_non_empty(value: ast.expr) -> bool:
    """True if a ``system=`` keyword value is provably non-empty."""
    if isinstance(value, ast.Constant):
        return value.value not in (None, "")
    # Name / Call / JoinedStr / etc — assume non-empty (best-effort).
    return True


def _options_call_has_system(call: ast.Call) -> bool:
    if _callable_name(call) != _OPTIONS_BUILDER:
        return False
    for kw in call.keywords:
        if kw.arg == "system" and _is_system_kw_non_empty(kw.value):
            return True
    return False


def _messages_has_system_literal(value: ast.expr) -> bool:
    """Best-effort static inspection of a ``messages=[...]`` literal."""
    if not isinstance(value, ast.List):
        return False
    for elt in value.elts:
        if not isinstance(elt, ast.Dict):
            continue
        for k, v in zip(elt.keys, elt.values, strict=False):
            if (
                isinstance(k, ast.Constant)
                and k.value == "role"
                and isinstance(v, ast.Constant)
                and v.value == "system"
            ):
                return True
    return False


def _resolve_name_to_options(func_body: list[ast.stmt], name: str) -> bool:
    """Walk ``func_body`` for ``name = ChatCompletionOptions(system=...)``.

    The lookup is intentionally local to the enclosing function — we do not
    cross module boundaries. The carve-out comment is the escape hatch.
    """
    for node in ast.walk(ast.Module(body=func_body, type_ignores=[])):
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id == name]
            if (
                targets
                and isinstance(node.value, ast.Call)
                and _options_call_has_system(node.value)
            ):
                return True
    return False


def _is_split_role_shape(call: ast.Call, enclosing_body: list[ast.stmt] | None) -> bool:
    for kw in call.keywords:
        if kw.arg == "options":
            if isinstance(kw.value, ast.Call) and _options_call_has_system(kw.value):
                return True
            if isinstance(kw.value, ast.Name) and enclosing_body is not None:
                if _resolve_name_to_options(enclosing_body, kw.value.id):
                    return True
        if kw.arg == "messages" and _messages_has_system_literal(kw.value):
            return True
    return False


def _has_carveout_marker(source_lines: list[str], lineno: int) -> bool:
    # ``lineno`` is 1-indexed. Check the call's own line and the preceding one.
    for ln in (lineno, lineno - 1):
        if 1 <= ln <= len(source_lines) and _CARVEOUT_MARKER in source_lines[ln - 1]:
            return True
    return False


def _enclosing_function(
    tree: ast.Module, target: ast.Call
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find the innermost FunctionDef/AsyncFunctionDef containing ``target``."""
    candidate: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if child is target:
                    candidate = node
                    break
    return candidate


def _docstring_present(func: ast.FunctionDef | ast.AsyncFunctionDef | None) -> bool:
    if func is None:
        return False
    return bool(ast.get_docstring(func, clean=False))


def _violation(path: Path, lineno: int, reason: str) -> str:
    base = (
        f"{path}:{lineno}: call_llm_structured missing system-prompt split "
        f"(use options=ChatCompletionOptions(system=...) or add "
        f"'{_CARVEOUT_MARKER}' carve-out)"
    )
    if reason:
        return f"{base} [{reason}]"
    return base


def _scan_file(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    aliases = _import_aliases(tree) | {_TARGET_FUNC}
    source_lines = source.splitlines()
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _callable_name(node) not in aliases:
            continue
        func = _enclosing_function(tree, node)
        enclosing_body = func.body if func is not None else None
        if _is_split_role_shape(node, enclosing_body):
            continue
        if _has_carveout_marker(source_lines, node.lineno):
            if _docstring_present(func):
                continue
            violations.append(
                _violation(path, node.lineno, "carve-out missing docstring rationale")
            )
            continue
        violations.append(_violation(path, node.lineno, ""))

    return violations


def main(argv: list[str]) -> int:
    raw_roots = argv[1:] or list(_DEFAULT_ROOTS)
    roots = [Path(r) for r in raw_roots]
    files = _iter_py_files(roots)
    all_violations: list[str] = []
    for path in sorted(files):
        all_violations.extend(_scan_file(path))
    for v in all_violations:
        sys.stderr.write(v + "\n")
    return 1 if all_violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
