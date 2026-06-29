"""Validate that docs/contracts/ local links are not broken.

The check catches broken local links in the public contract docs so that
cross-references stay in sync as files are moved or renamed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PUBLIC_DOC_PATHS = [
    Path("docs/ENGINEERING_STANDARDS.md"),
    Path("docs/ARCHITECTURE.md"),
    Path("docs/known-residual-risks.md"),
    Path("docs/contracts/README.md"),
    Path("docs/contracts/01-settings.md"),
    Path("docs/contracts/02-pulse.md"),
    Path("docs/contracts/03-llm.md"),
    Path("docs/contracts/04-observability.md"),
    Path("docs/contracts/05-models-and-hardware.md"),
    Path("docs/contracts/07-testing.md"),
]

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _iter_local_links(path: Path) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in LINK_RE.finditer(line):
            target = match.group(1).split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            links.append((line_no, target))
    return links


def _link_exists(source: Path, target: str) -> bool:
    target_path = Path(target)
    if target_path.is_absolute():
        return target_path.exists()
    return (source.parent / target_path).resolve().exists()


def main() -> int:
    """Check local links in public contract docs and report broken ones to stderr.

    Returns
    -------
    int
        0 if all checks pass, 1 if any failure is found.
    """
    errors: list[str] = []

    for rel_path in PUBLIC_DOC_PATHS:
        path = ROOT / rel_path
        if not path.exists():
            errors.append(f"missing required doc: {rel_path}")
            continue

        for line_no, target in _iter_local_links(path):
            if not _link_exists(path, target):
                errors.append(f"{rel_path}:{line_no} has broken local link: {target}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("contract docs ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
