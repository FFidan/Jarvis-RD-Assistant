#!/usr/bin/env python3
"""Dependency license policy: classify, gate, and keep NOTICE honest.

Single source of truth for the release-time license checks. All commands read
the JSON produced by ``pip-licenses --format=json`` (Python) and
``license-checker-rseidelsohn --json`` (Node), so CI and local runs see the
same data.

Commands
--------
gate
    Exit non-zero if ANY dependency is *strong* copyleft (GPL / AGPL), which is
    incompatible with redistributing this Apache-2.0 project. Weak / file-level
    copyleft (LGPL / MPL / EUPL) is permitted for unmodified, dynamically linked
    dependencies and does NOT fail the gate.
check-notice
    Verify the committed NOTICE still describes the installed environment: every
    weak-copyleft dependency must be attributed with the matching version, and
    no strong-copyleft dependency may be present. Exit non-zero on drift so the
    NOTICE cannot silently fall out of date.
inventory
    Print a deterministic, grouped third-party license summary from the same
    pip-licenses JSON (useful as a reviewable release artifact).

Strong vs weak copyleft
-----------------------
``GPL`` is a substring of ``LGPL``, so a naive ``GPL`` match would wrongly flag
the LGPL dependencies this project ships. The matcher below excludes LGPL:

  * ``(?<!L)GPL`` matches the SPDX short forms ``GPL`` / ``AGPL`` but never
    ``LGPL`` (under re.IGNORECASE the look-behind also rejects a leading ``l``).
  * The spelled-out branch catches ``... General Public License`` unless a
    preceding ``Lesser`` / ``Library`` marks it as the lesser (LGPL) variant.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# --- license classification -------------------------------------------------

_STRONG_SPDX = re.compile(r"(?<!L)GPL", re.IGNORECASE)
_GENERAL_PUBLIC = re.compile(r"general public license", re.IGNORECASE)
_WEAK_SPELLED = re.compile(r"(lesser|library)[^.]*general public license", re.IGNORECASE)
_WEAK_COPYLEFT = re.compile(
    r"\b(LGPL|MPL|EUPL)\b|lesser general public|mozilla public", re.IGNORECASE
)
_UNRECOGNIZED = re.compile(r"^(UNKNOWN|LicenseRef-)", re.IGNORECASE)

# These NVIDIA runtime packages use non-SPDX metadata but have been reviewed
# against the vendor links recorded in their installed package metadata and NOTICE.
# Match both package and marker exactly; every other unknown remains fail-closed.
_REVIEWED_UNRECOGNIZED_PYTHON = {
    ("cuda-bindings", "LicenseRef-NVIDIA-SOFTWARE-LICENSE"),
    ("cuda-toolkit", "UNKNOWN"),
    ("nvidia-cublas", "LicenseRef-NVIDIA-Proprietary"),
    ("nvidia-cuda-runtime", "LicenseRef-NVIDIA-Proprietary"),
    ("nvidia-cudnn-cu13", "LicenseRef-NVIDIA-Proprietary"),
    ("nvidia-nccl-cu13", "LicenseRef-NVIDIA-Proprietary"),
    ("nvidia-nvshmem-cu13", "LicenseRef-NVIDIA-Proprietary"),
}


def is_unrecognized(license_str: str) -> bool:
    """True for empty, UNKNOWN, or LicenseRef- strings that cannot be classified."""
    s = (license_str or "").strip()
    return not s or bool(_UNRECOGNIZED.match(s))


def is_reviewed_unrecognized_python(name: str, license_str: str) -> bool:
    """Return whether an exact package/license marker received manual review."""
    return (name.lower(), (license_str or "").strip()) in _REVIEWED_UNRECOGNIZED_PYTHON


def is_strong_copyleft(license_str: str) -> bool:
    """True for GPL / AGPL (strong copyleft); False for LGPL / MPL / EUPL / permissive."""
    s = license_str or ""
    if _STRONG_SPDX.search(s):
        return True
    if _GENERAL_PUBLIC.search(s) and not _WEAK_SPELLED.search(s):
        return True
    return False


def is_weak_copyleft(license_str: str) -> bool:
    """True for LGPL / MPL / EUPL (weak or file-level copyleft) that must be attributed."""
    s = license_str or ""
    if is_strong_copyleft(s):
        return False
    return bool(_WEAK_COPYLEFT.search(s))


# --- data loading -----------------------------------------------------------


def load_python(path: str) -> list[dict]:
    """Load ``pip-licenses --format=json`` output: a list of dicts with
    Name / Version / License keys."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON array from pip-licenses")
    return data


def load_node(path: str) -> dict:
    """Load ``license-checker-rseidelsohn --json`` output: a mapping of
    ``name@version`` -> {"licenses": ...}."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object from license-checker")
    return data


def _first_line(value: str) -> str:
    return (value or "").splitlines()[0] if value else ""


# --- gate -------------------------------------------------------------------


def cmd_gate(args: argparse.Namespace) -> int:
    hits: list[tuple[str, str, str, str]] = []
    unknown: list[tuple[str, str, str, str]] = []

    for dep in load_python(args.python_json):
        lic = dep.get("License", "")
        name = dep.get("Name", "?")
        ver = dep.get("Version", "?")
        if is_strong_copyleft(lic):
            hits.append(("python", name, ver, _first_line(lic)))
        elif is_unrecognized(lic) and not is_reviewed_unrecognized_python(name, lic):
            unknown.append(("python", name, ver, lic or "(empty)"))

    if args.node_json:
        node_path = Path(args.node_json)
        if node_path.exists():
            for pkg, info in load_node(args.node_json).items():
                lic = str(info.get("licenses", ""))
                if is_strong_copyleft(lic):
                    hits.append(("node", pkg, "", _first_line(lic)))
                elif is_unrecognized(lic):
                    unknown.append(("node", pkg, "", lic or "(empty)"))
        else:
            print(f"note: node license file not found, skipping: {args.node_json}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as fh:
            fh.write("## License gate (strong copyleft = fail; unrecognized = fail)\n\n")
            if hits:
                fh.write("Strong-copyleft (GPL / AGPL) dependencies block the release:\n\n")
                fh.write("| Ecosystem | Package | Version | License |\n|---|---|---|---|\n")
                for eco, name, ver, lic in sorted(hits):
                    fh.write(f"| {eco} | {name} | {ver} | {lic} |\n")
            if unknown:
                fh.write("\nUnrecognized license strings require manual triage:\n\n")
                fh.write("| Ecosystem | Package | Version | License |\n|---|---|---|---|\n")
                for eco, name, ver, lic in sorted(unknown):
                    fh.write(f"| {eco} | {name} | {ver} | {lic} |\n")
            if not hits and not unknown:
                fh.write(
                    "No strong-copyleft (GPL / AGPL) or unrecognized-license dependencies found.\n"
                )

    for eco, name, ver, lic in sorted(hits):
        print(
            f"::error title=Strong copyleft license::{eco} dependency "
            f"'{name}' {ver} is {lic} (GPL/AGPL) — incompatible with Apache-2.0 redistribution"
        )
    for eco, name, ver, lic in sorted(unknown):
        print(
            f"::error title=Unrecognized license::{eco} dependency "
            f"'{name}' {ver} has unrecognized license '{lic}' — verify upstream or add to allowlist"
        )

    total = len(hits) + len(unknown)
    if total:
        print(
            f"License gate FAILED: {len(hits)} strong-copyleft "
            f"+ {len(unknown)} unrecognized-license dependency(ies)."
        )
        return 1

    print(
        "License gate passed: no strong-copyleft (GPL/AGPL) or unrecognized-license dependencies."
    )
    return 0


# --- check-notice -----------------------------------------------------------

# NOTICE attribution lines, e.g. "  frozendict        2.4.7   — LGPL-3.0".
_NOTICE_PKG = re.compile(r"^\s{2,}(\S+)\s+(\S+)\s+—\s+\S")


def parse_notice_packages(notice_text: str) -> dict[str, str]:
    pkgs: dict[str, str] = {}
    for line in notice_text.splitlines():
        m = _NOTICE_PKG.match(line)
        if m:
            pkgs[m.group(1)] = m.group(2)
    return pkgs


def cmd_check_notice(args: argparse.Namespace) -> int:
    python_deps = load_python(args.python_json)
    notice_text = Path(args.notice).read_text()
    notice_pkgs = parse_notice_packages(notice_text)

    failures: list[str] = []

    strong = [
        (d.get("Name", "?"), _first_line(d.get("License", "?")))
        for d in python_deps
        if is_strong_copyleft(d.get("License", ""))
    ]
    if strong:
        failures.append(
            "strong-copyleft dependencies present (must not ship): "
            + ", ".join(f"{n} ({lic})" for n, lic in strong)
        )

    weak_python = {
        d.get("Name", ""): d.get("Version", "")
        for d in python_deps
        if is_weak_copyleft(d.get("License", ""))
    }

    missing_python = sorted(set(weak_python) - set(notice_pkgs))
    if missing_python:
        failures.append(
            "weak-copyleft Python dependencies not attributed in NOTICE: "
            + ", ".join(missing_python)
        )

    drift = [
        f"{name}: NOTICE={notice_pkgs[name]} installed={ver}"
        for name, ver in sorted(weak_python.items())
        if name in notice_pkgs and notice_pkgs[name] != ver
    ]
    if drift:
        failures.append("attributed version out of date: " + "; ".join(drift))

    reviewed_python = {
        d.get("Name", ""): d.get("Version", "")
        for d in python_deps
        if is_reviewed_unrecognized_python(d.get("Name", ""), d.get("License", ""))
    }
    missing_reviewed = sorted(set(reviewed_python) - set(notice_pkgs))
    if missing_reviewed:
        failures.append(
            "reviewed proprietary/custom-license dependencies not attributed in NOTICE: "
            + ", ".join(missing_reviewed)
        )
    reviewed_drift = [
        f"{name}: NOTICE={notice_pkgs[name]} installed={ver}"
        for name, ver in sorted(reviewed_python.items())
        if name in notice_pkgs and notice_pkgs[name] != ver
    ]
    if reviewed_drift:
        failures.append("reviewed dependency version out of date: " + "; ".join(reviewed_drift))

    # Node check (optional; pass --node-json to enable)
    if args.node_json:
        node_path = Path(args.node_json)
        if node_path.exists():
            for pkg, info in load_node(args.node_json).items():
                lic = str(info.get("licenses", ""))
                if is_weak_copyleft(lic):
                    # Use package name without @version suffix for NOTICE lookup
                    bare = pkg.rsplit("@", 1)[0] if "@" in pkg else pkg
                    if bare not in notice_pkgs:
                        failures.append(
                            f"Node weak-copyleft dependency not attributed in NOTICE: {pkg} ({lic})"
                        )
        else:
            print(f"note: node license file not found, skipping: {args.node_json}")

    if failures:
        print("NOTICE is out of date with the installed environment:")
        for f in failures:
            print(f"  - {f}")
        print("Regenerate the affected NOTICE entries and commit the result.")
        return 1

    node_checked = " + Node" if args.node_json else ""
    print(
        f"NOTICE is consistent: {len(weak_python)} Python{node_checked} "
        "weak-copyleft dependencies attributed, "
        "no strong-copyleft dependencies present."
    )
    return 0


# --- inventory --------------------------------------------------------------


def _category(license_str: str) -> str:
    if is_strong_copyleft(license_str):
        return "Strong copyleft (GPL/AGPL)"
    if is_weak_copyleft(license_str):
        return "Weak / file-level copyleft (LGPL/MPL/EUPL)"
    return "Permissive / other"


def cmd_inventory(args: argparse.Namespace) -> int:
    deps = load_python(args.python_json)
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for dep in deps:
        cat = _category(dep.get("License", ""))
        groups.setdefault(cat, []).append(
            (dep.get("Name", "?"), dep.get("Version", "?"), _first_line(dep.get("License", "?")))
        )

    print(f"Third-party Python license inventory ({len(deps)} packages)\n")
    for cat in sorted(groups):
        print(cat)
        for name, ver, lic in sorted(groups[cat]):
            print(f"  {name}  {ver}  {lic}")
        print()
    return 0


# --- cli --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gate = sub.add_parser("gate", help="fail on strong-copyleft (GPL/AGPL) dependencies")
    p_gate.add_argument("--python-json", required=True)
    p_gate.add_argument("--node-json")
    p_gate.set_defaults(func=cmd_gate)

    p_check = sub.add_parser("check-notice", help="verify NOTICE matches the installed env")
    p_check.add_argument("--python-json", required=True)
    p_check.add_argument("--node-json")
    p_check.add_argument("--notice", default="NOTICE")
    p_check.set_defaults(func=cmd_check_notice)

    p_inv = sub.add_parser("inventory", help="print a deterministic license inventory")
    p_inv.add_argument("--python-json", required=True)
    p_inv.set_defaults(func=cmd_inventory)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
