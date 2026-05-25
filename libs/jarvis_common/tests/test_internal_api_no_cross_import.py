"""Lint-style guard: jarvis_common.testing_embedder must NOT module-level import from any service."""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_PREFIXES = ("paper_ingestion", "learning_engine", "telegram_bot")


def test_testing_embedder_has_no_top_level_service_imports() -> None:
    src = Path("libs/jarvis_common/jarvis_common/testing_embedder.py").read_text()
    tree = ast.parse(src)
    violations: list[str] = []
    for node in tree.body:  # top-level only
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in _FORBIDDEN_PREFIXES:
                violations.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _FORBIDDEN_PREFIXES:
                    violations.append(f"line {node.lineno}: import {alias.name}")
    assert not violations, "Top-level service imports in testing_embedder.py: " + "; ".join(
        violations
    )
