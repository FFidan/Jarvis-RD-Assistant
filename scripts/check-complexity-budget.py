#!/usr/bin/env python3
"""No-growth complexity ratchet.

Runs ruff's complexity rules (kept OUT of [tool.ruff.lint] select so the main
lint stays actionable) and fails when any rule's violation count exceeds the
frozen baseline in pyproject's [tool.complexity-budget]. New code therefore
cannot add complexity; an improvement prints a nudge to lower the baseline but
never fails (mirrors the frontend eslint --max-warnings budget).

Exit code: 0 = within budget, 1 = a rule grew or ruff could not be measured.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGETS = ("services", "libs", "scripts")
# Tests are excluded so the budget tracks production complexity only; a long
# fixture or a many-argument test helper must not consume production headroom.
_EXCLUDE = "**/tests/**"
# Lowercased fragments of the messages ruff emits when it cannot read something.
# It reports these as warnings and still exits 0/1, so they are the only signal
# that a zero count means "nothing was scanned" rather than "nothing was found".
# Matching these rather than any stderr output keeps `uv run`'s own resolve and
# install notices, and ruff's benign rule-selection warnings, from failing the gate.
_SCAN_FAILURE_MARKERS = ("failed to lint", "encountered error", "os error")


def baseline() -> dict[str, int]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return dict(data["tool"]["complexity-budget"]["max"])


def measure(codes: list[str]) -> dict[str, int]:
    # A target that no longer resolves is not an error to ruff, so renaming or
    # moving one of these directories would zero every count and disable the
    # ratchet for the whole project while still reporting success.
    missing = [target for target in _TARGETS if not (_ROOT / target).exists()]
    if missing:
        raise SystemExit(
            f"complexity budget cannot be measured: target(s) {', '.join(missing)} do not exist "
            f"under {_ROOT} — update _TARGETS if a directory was renamed or moved"
        )
    proc = subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            "--select",
            ",".join(codes),
            "--extend-exclude",
            _EXCLUDE,
            "--statistics",
            "--output-format",
            "json",
            *_TARGETS,
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # Ruff exits 0 clean / 1 with violations; anything else is a tool error that
    # leaves stdout empty, which would otherwise measure as zero complexity and
    # pass the gate.
    if proc.returncode not in (0, 1):
        raise SystemExit(
            f"ruff failed (exit {proc.returncode}); complexity budget cannot be measured: "
            f"{proc.stderr.strip()[:300]}"
        )
    # A path ruff cannot read is only a warning to it: it still exits 0 or 1 and
    # silently drops that subtree, so the counts shrink and the gate congratulates
    # a ratchet down over code nobody measured. This catches unreadable files and
    # nested directories, which the target check above cannot see.
    if any(marker in proc.stderr.lower() for marker in _SCAN_FAILURE_MARKERS):
        raise SystemExit(
            "ruff could not read every target; complexity budget cannot be measured: "
            f"{proc.stderr.strip()[:300]}"
        )
    rows = json.loads(proc.stdout or "[]")
    counts = {code: 0 for code in codes}
    counts.update({row["code"]: row["count"] for row in rows})
    return counts


def main() -> int:
    caps = baseline()
    counts = measure(sorted(caps))
    grew = [
        f"  {code}: {counts[code]} > budget {cap} (+{counts[code] - cap})"
        for code, cap in sorted(caps.items())
        if counts[code] > cap
    ]
    shrank = [
        f"  {code}: {counts[code]} < budget {cap} — lower the cap to {counts[code]}"
        for code, cap in sorted(caps.items())
        if counts[code] < cap
    ]
    if grew:
        print("Complexity budget exceeded — extract a helper instead of growing a function:")
        print("\n".join(grew))
        return 1
    if shrank:
        print("Complexity improved — ratchet [tool.complexity-budget] down:")
        print("\n".join(shrank))
    print(f"Complexity within budget: {dict(sorted(counts.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
