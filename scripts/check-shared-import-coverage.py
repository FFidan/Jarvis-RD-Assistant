#!/usr/bin/env python3
"""Assert every runtime import of the shared library is a declared dependency.

Service images install the ``jarvis_common`` wheel with ``--no-deps``
(``services/telegram_bot/Dockerfile:27`` and siblings), so the wheel's own
metadata buys nothing at build time: a consumer's hash-pinned ``constraints.txt``
is the ONLY thing that can satisfy an import ``jarvis_common`` performs. A
distribution the shared library imports at module scope but nobody declares
therefore produces an image that installs cleanly and crash-loops on startup.

For every module-scope third-party import in the shared library's runtime
modules, this checks that the distribution is

1. declared in the ``jarvis-common`` dependency group of the root
   ``pyproject.toml`` -- the group every consumer includes, so declaring it
   there fixes all consumers at once; and
2. pinned in each base ``constraints.txt`` a service image installs.

``testing.py``, ``testing_*`` modules and ``testing_sidecars/`` are not scanned
on their own: they ship inside the wheel but no service imports them at runtime,
and declaring
``pytest`` a runtime dependency of every image would be wrong. They are not
exempt, though. Intra-package imports are followed transitively, so the moment a
runtime module imports a testing helper (directly or through a chain), that
helper's own import-time requirements count -- the interpreter will execute them
on service startup. Purely test-side imports remain covered by the release-time
smoke that runs each image's own interpreter.

Imports that are deferred (inside a function or class), guarded by an
``ImportError`` handler, or reachable only under ``TYPE_CHECKING`` are not
import-time requirements and are skipped.

Exit code: 0 = clean, 1 = undeclared imports found, 2 = usage error.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

SHARED_PACKAGE = Path("libs/jarvis_common/jarvis_common")
ROOT_PYPROJECT = Path("pyproject.toml")
OWNING_GROUP = "jarvis-common"

# Base constraint sets: each is installed on its own as a service image's entire
# third-party dependency set, so each must satisfy the shared library by itself.
# The paper-ingestion image selects between two of them via TORCH_VARIANT
# (services/paper_ingestion/Dockerfile:51-52). Additive sets layered on top of a
# base (constraints-optional*.txt, constraints-profiling.txt) are not listed --
# they cannot be the sole source of an import.
BASE_CONSTRAINTS = (
    Path("services/platform_api/constraints.txt"),
    Path("services/telegram_bot/constraints.txt"),
    Path("services/learning_engine/constraints.txt"),
    Path("services/paper_ingestion/constraints.txt"),
    Path("services/paper_ingestion/constraints-cpu.txt"),
)

# Import-package name -> PyPI distribution name, for the cases where lowercasing
# and swapping underscores for hyphens does not get there. `opentelemetry` is a
# namespace package many distributions contribute to; the shared library imports
# `opentelemetry.sdk.trace`, which only opentelemetry-sdk provides.
DISTRIBUTION_OVERRIDES = {
    "opentelemetry": "opentelemetry-sdk",
}


def normalize(name: str) -> str:
    """Return *name* in PEP 503 normalized form."""
    return name.lower().replace("_", "-").replace(".", "-")


def distribution_for(import_root: str) -> str:
    """Return the distribution expected to provide top-level package *import_root*."""
    return DISTRIBUTION_OVERRIDES.get(import_root, normalize(import_root))


def _guards_import_error(node: ast.Try) -> bool:
    """Return True if *node* handles ImportError, making its body's imports optional."""
    for handler in node.handlers:
        exc = handler.type
        names = []
        if isinstance(exc, ast.Name):
            names = [exc.id]
        elif isinstance(exc, ast.Tuple):
            names = [elt.id for elt in exc.elts if isinstance(elt, ast.Name)]
        if {"ImportError", "ModuleNotFoundError"} & set(names):
            return True
    return False


def _is_type_checking(test: ast.expr) -> bool:
    """Return True if *test* is the TYPE_CHECKING guard."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _skipped_at_import_time(node: ast.AST) -> bool:
    """Return True if *node*'s body does not run when the module is imported."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return True  # deferred to call time, not an import-time requirement
    # An ImportError handler is the fallback that makes its body's imports optional.
    return isinstance(node, ast.Try) and _guards_import_error(node)


def _imported_modules(node: ast.AST, current_package: str) -> list[tuple[str, int]]:
    """Return (dotted module, line) for the modules an import statement pulls in.

    ``from X import name`` also yields ``X.name``: when ``name`` is a submodule
    rather than an attribute, importing it executes that submodule too. Names
    that are plain attributes resolve to no file and drop out downstream.
    Relative imports are resolved against *current_package*.
    """
    if isinstance(node, ast.Import):
        return [(alias.name, node.lineno) for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level == 0:
        base = node.module or ""
    else:
        parts = current_package.split(".")
        if node.level > len(parts):
            return []  # escapes the package; nothing this scan can resolve
        base = ".".join(parts[: len(parts) - node.level + 1])
        if node.module:
            base = f"{base}.{node.module}"
    if not base:
        return []
    found = [(base, node.lineno)]
    found += [(f"{base}.{alias.name}", node.lineno) for alias in node.names if alias.name != "*"]
    return found


def import_time_modules(tree: ast.Module, current_package: str) -> list[tuple[str, int]]:
    """Return (dotted module, line) for every import executed on module import."""
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if _skipped_at_import_time(child):
                continue
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for fallback in child.orelse:  # only the runtime arm of the guard
                    visit(fallback)
                continue
            modules = _imported_modules(child, current_package)
            if modules:
                found.extend(modules)
            else:
                visit(child)

    visit(tree)
    return found


def runtime_modules(package: Path) -> list[Path]:
    """Return the shared library's runtime modules, excluding test-only helpers.

    ``testing.py`` is the aggregator that re-exports the ``testing_*`` helpers,
    so it is excluded on the same grounds. Exclusion only keeps these out of the
    scan's roots; anything a runtime module imports is still followed.
    """
    return [
        path
        for path in sorted(package.rglob("*.py"))
        if not path.name.startswith("testing_")
        and path.name != "testing.py"
        and "testing_sidecars" not in path.parts
    ]


def _module_package(package: Path, path: Path) -> str:
    """Return the dotted package containing *path*, for relative-import resolution."""
    parts = path.relative_to(package.parent).with_suffix("").parts
    return ".".join(parts[:-1])


def _module_files(package: Path, dotted: str) -> list[Path]:
    """Return the source files that importing intra-package module *dotted* executes.

    Includes every ancestor package ``__init__.py`` on the way down, because
    importing a submodule runs them all. A dotted name that resolves to no
    module file (an attribute pulled in via ``from X import name``) contributes
    only the ancestors that exist.
    """
    files = [package / "__init__.py"]
    base = package
    for part in dotted.split(".")[1:]:
        base = base / part
        files.append(base.with_suffix(".py"))
        files.append(base / "__init__.py")
    return [candidate for candidate in files if candidate.is_file()]


def required_distributions(root: Path) -> dict[str, str]:
    """Map each required distribution to the first ``file:line`` that imports it.

    Starts from the runtime modules and follows intra-package imports
    transitively, so a module the scan does not seed (``testing_*``) still has
    its import-time requirements counted once anything runtime imports it.
    """
    first_use: dict[str, str] = {}
    package = root / SHARED_PACKAGE
    queue = runtime_modules(package)
    scanned = set(queue)
    while queue:
        path = queue.pop(0)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in import_time_modules(tree, _module_package(package, path)):
            import_root = module.split(".")[0]
            if import_root == "__future__" or import_root in sys.stdlib_module_names:
                continue
            if import_root == SHARED_PACKAGE.name:
                for target in _module_files(package, module):
                    if target not in scanned:
                        scanned.add(target)
                        queue.append(target)
                continue
            first_use.setdefault(
                distribution_for(import_root),
                f"{path.relative_to(root)}:{lineno}",
            )
    return first_use


def declared_in_group(root: Path) -> set[str]:
    """Return the distributions declared in the owning dependency group."""
    groups = tomllib.loads((root / ROOT_PYPROJECT).read_text(encoding="utf-8"))
    declared = set()
    for entry in groups["dependency-groups"][OWNING_GROUP]:
        if isinstance(entry, str):
            # "procrastinate[aiopg]>=0.49" -> "procrastinate"
            name = entry.split(";")[0].strip()
            for separator in ("[", "=", ">", "<", "!", "~", " "):
                name = name.split(separator)[0]
            declared.add(normalize(name))
    return declared


def pinned_in(constraints: Path) -> set[str]:
    """Return the distributions pinned in a ``--require-hashes`` constraints file."""
    pinned = set()
    for line in constraints.read_text(encoding="utf-8").splitlines():
        if line.startswith(("#", " ", "-", "\t")) or "==" not in line:
            continue
        pinned.add(normalize(line.split("==")[0].split("[")[0].strip()))
    return pinned


def _unmeasurable(root: Path) -> str | None:
    """Return why this check cannot be performed, or None if every input resolves.

    Without this, a moved or renamed input turns the check into a no-op that
    still exits 0: an absent package scans zero modules and reports "OK 0
    distributions". A gate that cannot see its subject must fail, not pass --
    the defect it exists to catch shipped in four releases behind exactly that
    shape of silent success.
    """
    package = root / SHARED_PACKAGE
    if not package.is_dir():
        return f"{SHARED_PACKAGE} does not exist"
    if not runtime_modules(package):
        return f"{SHARED_PACKAGE} contains no runtime modules to scan"
    missing = [str(c) for c in BASE_CONSTRAINTS if not (root / c).is_file()]
    if missing:
        return f"constraint set(s) not found: {', '.join(missing)}"
    try:
        declared_in_group(root)
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        return f"cannot read the '{OWNING_GROUP}' group from {ROOT_PYPROJECT}: {exc}"
    return None


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(f"usage: {Path(argv[0]).name} [repo-root]", file=sys.stderr)
        return 2
    root = Path(argv[1] if len(argv) == 2 else ".").resolve()

    if reason := _unmeasurable(root):
        print(
            f"shared-library import coverage cannot be measured: {reason}.\n"
            "Re-point this check at the moved input rather than ignoring it.",
            file=sys.stderr,
        )
        return 2

    required = required_distributions(root)
    declared = declared_in_group(root)
    violations: list[str] = []

    for distribution, source in sorted(required.items()):
        if distribution not in declared:
            violations.append(
                f"{source}: imports {distribution}, which the '{OWNING_GROUP}' dependency "
                f"group in {ROOT_PYPROJECT} does not declare"
            )
    for constraints in BASE_CONSTRAINTS:
        pinned = pinned_in(root / constraints)
        for distribution, source in sorted(required.items()):
            if distribution not in pinned:
                violations.append(
                    f"{source}: imports {distribution}, which {constraints} does not pin -- "
                    f"the image built from it cannot import jarvis_common"
                )

    if violations:
        print("Undeclared runtime dependencies of the shared library:\n", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            f"\nDeclare each one in the '{OWNING_GROUP}' group of {ROOT_PYPROJECT}, then run "
            "scripts/export-service-requirements.sh to regenerate the pinned files.\n"
            "If the distribution name simply differs from the import name, add it to "
            f"DISTRIBUTION_OVERRIDES in {Path(argv[0]).name} instead.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK {len(required)} distributions imported by jarvis_common are declared in "
        f"'{OWNING_GROUP}' and pinned in {len(BASE_CONSTRAINTS)} base constraint sets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
