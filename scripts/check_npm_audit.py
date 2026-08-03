#!/usr/bin/env python3
"""Enforce expiring, exposure-checked exceptions around ``npm audit``."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT / "frontend"
POLICY_PATH = FRONTEND_ROOT / "osv-scanner.toml"

REACT_ROUTER_RSC_ADVISORY = "GHSA-qwww-vcr4-c8h2"
SUPPORTED_EXCEPTIONS = {REACT_ROUTER_RSC_ADVISORY}
FAIL_SEVERITIES = {"high", "critical"}
GHSA_URL_PATTERN = re.compile(r"/(GHSA-[0-9A-Za-z-]+)$")
RSC_USAGE_PATTERN = re.compile(
    r"\b(?:RSC(?:Static|Hydrated)?Router|createCallServer|getRSCStream|"
    r"routeRSCServerRequest|decodeAction|decodeReply)\b|react-router/dom/server"
)
FRONTEND_CODE_SUFFIXES = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
IGNORED_FRONTEND_DIRS = {
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}


class AuditPolicyError(RuntimeError):
    """Raised when the audit result cannot satisfy the checked policy."""


@dataclass(frozen=True)
class ExceptionEntry:
    """One expiring OSV/npm advisory exception."""

    advisory_id: str
    expires: date
    reason: str


def _as_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuditPolicyError(f"{label} must be a JSON object")
    return value


def _load_json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditPolicyError(f"cannot read {label}: {exc}") from exc
    return _as_mapping(document, label)


def _parse_exception_entry(value: object, index: int, *, today: date) -> ExceptionEntry:
    raw = _as_mapping(value, f"IgnoredVulns[{index}]")
    advisory_id = raw.get("id")
    reason = raw.get("reason")
    expires = raw.get("ignoreUntil")
    if not isinstance(advisory_id, str) or not advisory_id:
        raise AuditPolicyError(f"IgnoredVulns[{index}].id must be non-empty")
    if advisory_id not in SUPPORTED_EXCEPTIONS:
        raise AuditPolicyError(f"{advisory_id} has no exposure validator")
    if isinstance(expires, datetime):
        expires = expires.date()
    if not isinstance(expires, date):
        raise AuditPolicyError(f"{advisory_id} must have an ignoreUntil date")
    if expires <= today:
        raise AuditPolicyError(f"{advisory_id} exception expired on {expires.isoformat()}")
    if not isinstance(reason, str) or not reason.strip():
        raise AuditPolicyError(f"{advisory_id} must have a reason")
    return ExceptionEntry(advisory_id, expires, reason.strip())


def _load_policy(path: Path, *, today: date) -> dict[str, ExceptionEntry]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditPolicyError(f"cannot read audit policy {path}: {exc}") from exc

    raw_entries = document.get("IgnoredVulns")
    if not isinstance(raw_entries, list):
        raise AuditPolicyError("audit policy must define [[IgnoredVulns]] entries")

    entries: dict[str, ExceptionEntry] = {}
    for index, value in enumerate(raw_entries):
        entry = _parse_exception_entry(value, index, today=today)
        if entry.advisory_id in entries:
            raise AuditPolicyError(f"{entry.advisory_id} is listed more than once")
        entries[entry.advisory_id] = entry

    return entries


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


def _validate_rsc_absent(repo_root: Path) -> None:
    frontend = repo_root / "frontend"
    package = _load_json_mapping(frontend / "package.json", "frontend/package.json")

    direct_packages: set[str] = set()
    for section_name in ("dependencies", "devDependencies"):
        section = _as_mapping(package.get(section_name), section_name)
        direct_packages.update(section)
    forbidden_packages = sorted(
        name
        for name in direct_packages
        if name == "react-router" or name.startswith("@react-router/")
    )
    if forbidden_packages:
        raise AuditPolicyError(
            "React Router RSC exception is invalid with direct packages: "
            + ", ".join(forbidden_packages)
        )

    if any(frontend.glob("react-router.config.*")):
        raise AuditPolicyError("React Router RSC exception is invalid with framework configuration")

    for path in frontend.rglob("*"):
        relative_frontend_path = path.relative_to(frontend)
        if any(part in IGNORED_FRONTEND_DIRS for part in relative_frontend_path.parts):
            continue
        if not path.is_file() or path.suffix not in FRONTEND_CODE_SUFFIXES:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AuditPolicyError(f"cannot inspect {path.relative_to(repo_root)}: {exc}") from exc
        if RSC_USAGE_PATTERN.search(source):
            relative_path = path.relative_to(repo_root)
            raise AuditPolicyError(
                f"React Router RSC exception is invalid with RSC usage in {relative_path}"
            )


def evaluate_reports(
    full_report: object,
    production_report: object,
    *,
    repo_root: Path = REPO_ROOT,
    policy_path: Path = POLICY_PATH,
    today: date | None = None,
) -> tuple[ExceptionEntry, ...]:
    """Validate full and production npm reports against the shared OSV policy."""
    effective_today = today or date.today()
    policy = _load_policy(policy_path, today=effective_today)
    full_ids = _high_advisory_ids(full_report)
    production_ids = _high_advisory_ids(production_report)

    unexpected = sorted(full_ids - policy.keys())
    if unexpected:
        raise AuditPolicyError("unaccepted high-severity advisories: " + ", ".join(unexpected))
    stale = sorted(policy.keys() - full_ids)
    if stale:
        raise AuditPolicyError("remove resolved audit exceptions: " + ", ".join(stale))

    if REACT_ROUTER_RSC_ADVISORY not in production_ids:
        raise AuditPolicyError("React Router RSC exception is not present in the production audit")
    unexpected_production = sorted(production_ids - {REACT_ROUTER_RSC_ADVISORY})
    if unexpected_production:
        raise AuditPolicyError(
            "unexpected production advisories: " + ", ".join(unexpected_production)
        )

    _validate_rsc_absent(repo_root)
    return tuple(policy[key] for key in sorted(policy))


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
    """Run both npm audit scopes and enforce the checked exceptions."""
    try:
        entries = evaluate_reports(
            _run_npm_audit(omit_dev=False),
            _run_npm_audit(omit_dev=True),
        )
    except AuditPolicyError as exc:
        print(f"npm audit policy: FAIL: {exc}", file=sys.stderr)
        return 1

    for entry in entries:
        print(f"accepted {entry.advisory_id} until {entry.expires.isoformat()}: {entry.reason}")
    print("npm audit policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
