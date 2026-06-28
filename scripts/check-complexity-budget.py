#!/usr/bin/env python3
"""No-growth complexity ratchet.

Runs ruff's complexity rules (kept OUT of [tool.ruff.lint] select so the main
lint stays actionable) and fails when any rule's violation count exceeds the
frozen baseline in pyproject's [tool.complexity-budget]. New code therefore
cannot add complexity; an improvement prints a nudge to lower the baseline but
never fails (mirrors the frontend eslint --max-warnings budget).

Exit code: 0 = within budget, 1 = a rule grew.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGETS = ("services", "libs", "scripts")


def baseline() -> dict[str, int]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return dict(data["tool"]["complexity-budget"]["max"])


def measure(codes: list[str]) -> dict[str, int]:
    proc = subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            "--select",
            ",".join(codes),
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
