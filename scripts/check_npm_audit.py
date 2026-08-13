#!/usr/bin/env python3
"""Fail when npm reports a high or critical advisory in either audit scope."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT / "frontend"
FAIL_SEVERITIES = {"high", "critical"}
GHSA_URL_PATTERN = re.compile(r"/(GHSA-[0-9A-Za-z-]+)$")


class AuditPolicyError(RuntimeError):
    """Raised when npm output is invalid or contains a blocking advisory."""


def _as_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuditPolicyError(f"{label} must be a JSON object")
    return value


def _advisory_id(value: dict[str, Any]) -> str:
    url = value.get("url")
    if not isinstance(url, str):
        raise AuditPolicyError("high-severity npm advisory has no URL")
    match = GHSA_URL_PATTERN.search(url)
    if match is None:
        raise AuditPolicyError(f"high-severity npm advisory has an unsupported URL: {url}")
    return match.group(1)


def _resolve_advisory_roots(
    name: str,
    vulnerabilities: dict[str, Any],
    trail: frozenset[str],
) -> set[str]:
    if name in trail:
        raise AuditPolicyError(f"npm audit dependency cycle includes {name}")
    vulnerability = _as_mapping(vulnerabilities.get(name), f"vulnerability {name}")
    if vulnerability.get("severity") not in FAIL_SEVERITIES:
        return set()
    via = vulnerability.get("via")
    if not isinstance(via, list):
        raise AuditPolicyError(f"vulnerability {name}.via must be an array")

    roots: set[str] = set()
    for cause in via:
        if isinstance(cause, str):
            if cause not in vulnerabilities:
                raise AuditPolicyError(f"vulnerability {name} references missing cause {cause}")
            roots.update(_resolve_advisory_roots(cause, vulnerabilities, trail | {name}))
        elif isinstance(cause, dict) and cause.get("severity") in FAIL_SEVERITIES:
            roots.add(_advisory_id(cause))
    if not roots:
        raise AuditPolicyError(f"high-severity vulnerability {name} has no advisory root")
    return roots


def _high_advisory_ids(report: object) -> set[str]:
    document = _as_mapping(report, "npm audit report")
    if document.get("auditReportVersion") != 2:
        raise AuditPolicyError("npm audit report is missing auditReportVersion 2")
    vulnerabilities = _as_mapping(document.get("vulnerabilities"), "vulnerabilities")

    roots: set[str] = set()
    for name, value in vulnerabilities.items():
        vulnerability = _as_mapping(value, f"vulnerability {name}")
        if vulnerability.get("severity") in FAIL_SEVERITIES:
            roots.update(_resolve_advisory_roots(name, vulnerabilities, frozenset()))
    return roots


def evaluate_reports(full_report: object, production_report: object) -> None:
    """Require both npm audit scopes to be structurally valid and blocker-free."""
    full_ids = _high_advisory_ids(full_report)
    production_ids = _high_advisory_ids(production_report)
    blocking = sorted(full_ids | production_ids)
    if blocking:
        raise AuditPolicyError("high-severity advisories: " + ", ".join(blocking))


def _run_npm_audit(*, omit_dev: bool) -> object:
    command = ["npm", "audit", "--prefix", str(FRONTEND_ROOT), "--json"]
    if omit_dev:
        command.append("--omit=dev")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1}:
        raise AuditPolicyError(
            f"{' '.join(command)} exited {result.returncode}: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AuditPolicyError(f"{' '.join(command)} returned invalid JSON") from exc


def main() -> int:
    """Run complete and production-only npm audits with no advisory exceptions."""
    try:
        evaluate_reports(
            _run_npm_audit(omit_dev=False),
            _run_npm_audit(omit_dev=True),
        )
    except AuditPolicyError as exc:
        print(f"npm audit policy: FAIL: {exc}", file=sys.stderr)
        return 1
    print("npm audit policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
