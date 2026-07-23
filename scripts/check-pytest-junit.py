#!/usr/bin/env python3
"""Require a successful, non-skipped pytest JUnit result.

Release-critical integration jobs must execute at least one test. Pytest treats
an all-skipped selection as successful, so CI validates the report produced by
that selection instead of repeating collection with a separate command.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _count(root: ET.Element, attribute: str) -> int:
    """Sum an integer attribute across the report's leaf test suites."""
    if root.tag == "testsuite":
        suites = [root]
    else:
        suites = [suite for suite in root.iter("testsuite") if not suite.findall("testsuite")]
    return sum(int(suite.get(attribute, "0")) for suite in suites)


def validate_report(path: Path, label: str) -> list[str]:
    """Return release-gate failures found in one pytest JUnit report.

    Parameters
    ----------
    path : pathlib.Path
        JUnit XML report written by pytest.
    label : str
        Human-readable test-selection name used in diagnostics.

    Returns
    -------
    list[str]
        Validation failures. An empty list means the selection passed at least
        one test without failures, errors, or skips.

    """
    try:
        root = ET.parse(path).getroot()
        tests = _count(root, "tests")
        failures = _count(root, "failures")
        errors = _count(root, "errors")
        skipped = _count(root, "skipped")
    except (OSError, ET.ParseError, ValueError) as exc:
        return [f"{label}: unreadable JUnit report {path}: {exc}"]

    passed = tests - failures - errors - skipped
    print(
        f"{label}: passed={passed} skipped={skipped} "
        f"failures={failures} errors={errors} total={tests}"
    )
    failures_found: list[str] = []
    if passed <= 0:
        failures_found.append(f"{label}: no tests passed")
    if skipped:
        failures_found.append(f"{label}: {skipped} test(s) skipped")
    if failures or errors:
        failures_found.append(f"{label}: {failures} failure(s) and {errors} error(s) recorded")
    return failures_found


def main() -> int:
    """Validate the requested report.

    Returns
    -------
    int
        Zero when the report contains a passing, non-skipped selection;
        otherwise one.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="pytest JUnit XML report")
    parser.add_argument("--label", default="pytest selection", help="diagnostic label")
    args = parser.parse_args()

    failures = validate_report(args.report, args.label)
    for failure in failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
