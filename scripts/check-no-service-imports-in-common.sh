#!/usr/bin/env bash
# Guard: the shared library must not import a service package.
#
# jarvis_common is a dependency of platform_api, paper_ingestion,
# learning_engine and telegram_bot. An import in the other direction makes the
# library unusable without the service it reaches into, and it is invisible at
# review time when the import sits inside a function body.
#
# `uv run tach check` does not catch this. The service packages are installed
# distributions in the development environment, so tach resolves an import of
# one as an EXTERNAL dependency and never applies the `jarvis_common`
# `depends_on = []` rule to it. Verified by adding such an import and running
# `tach check`: it reports "All modules validated!" and lists the import under
# external dependencies. This scan is the enforcement.
#
# It parses the package instead of grepping it, so an import escapes only by
# not being an import: `try: import platform_api`, `from services.x import y`,
# `import json, telegram_bot` and function-body imports are all reported, and
# the words appearing in a comment or a docstring are not.
set -Eeuo pipefail

cd "$(dirname "$0")/.." || { echo "fatal: cannot cd to repo root" >&2; exit 1; }

TARGET="libs/jarvis_common/jarvis_common"
# A missing target would make the scan report success over nothing.
[ -d "$TARGET" ] || { echo "fatal: $TARGET does not exist" >&2; exit 1; }

python3 - "$TARGET" <<'PY'
"""Report every import of a service package inside the shared library."""

import ast
import sys
from pathlib import Path

SERVICE_PACKAGES = frozenset(
    {"platform_api", "paper_ingestion", "learning_engine", "telegram_bot"}
)

target = Path(sys.argv[1])


def service_package(dotted: str) -> str | None:
    """Return the service package a dotted module path reaches into, if any."""
    parts = dotted.split(".")
    if parts[0] in SERVICE_PACKAGES:
        return parts[0]
    # `services.paper_ingestion` is not importable in this layout, but it is
    # the shape a hand-written path would take, so it is reported too.
    if parts[0] == "services" and len(parts) > 1 and parts[1] in SERVICE_PACKAGES:
        return parts[1]
    return None


def violations(tree: ast.Module) -> list[tuple[int, str]]:
    """Return (line, package) for every static or dynamic service import."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                package = service_package(alias.name)
                if package:
                    found.append((node.lineno, package))
        elif isinstance(node, ast.ImportFrom):
            # `level` marks a relative import, which cannot leave the package.
            if node.module and not node.level:
                package = service_package(node.module)
                if package:
                    found.append((node.lineno, package))
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            if name != "import_module" or not node.args:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                package = service_package(argument.value)
                if package:
                    found.append((node.lineno, package))
    return found


failed = False
for path in sorted(target.rglob("*.py")):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"{path}: could not parse ({exc})", file=sys.stderr)
        failed = True
        continue
    for lineno, package in sorted(violations(tree)):
        failed = True
        print(f"{path}:{lineno}: imports {package}")

if failed:
    print("ERROR: jarvis_common must not import a service package.", file=sys.stderr)
    print("  Inject the service object as a parameter instead.", file=sys.stderr)
    raise SystemExit(1)

print("check-no-service-imports-in-common: OK")
PY
