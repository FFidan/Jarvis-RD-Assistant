"""Regression tests for the fail-closed frontend npm audit parser."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_npm_audit.py"

_spec = importlib.util.spec_from_file_location("check_npm_audit", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_audit_policy = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, _audit_policy)
_spec.loader.exec_module(_audit_policy)

AuditPolicyError = _audit_policy.AuditPolicyError
evaluate_reports = _audit_policy.evaluate_reports


def _advisory(advisory_id: str, dependency: str) -> dict[str, object]:
    return {
        "source": 1,
        "name": dependency,
        "dependency": dependency,
        "title": f"{dependency} advisory",
        "url": f"https://github.com/advisories/{advisory_id}",
        "severity": "high",
    }


def _report(*, advisory_id: str | None = None) -> dict[str, object]:
    vulnerabilities: dict[str, object] = {}
    if advisory_id is not None:
        vulnerabilities = {
            "affected-root": {
                "severity": "high",
                "via": [_advisory(advisory_id, "affected-root")],
            },
            "affected-dependent": {
                "severity": "high",
                "via": ["affected-root"],
            },
        }
    return {"auditReportVersion": 2, "vulnerabilities": vulnerabilities}


def test_clean_full_and_production_reports_pass() -> None:
    evaluate_reports(_report(), _report())


def test_any_high_advisory_fails_without_an_exception_path() -> None:
    with pytest.raises(AuditPolicyError, match="GHSA-1111-2222-3333"):
        evaluate_reports(_report(advisory_id="GHSA-1111-2222-3333"), _report())


def test_production_only_advisory_also_fails() -> None:
    with pytest.raises(AuditPolicyError, match="GHSA-4444-5555-6666"):
        evaluate_reports(_report(), _report(advisory_id="GHSA-4444-5555-6666"))


def test_malformed_audit_report_fails() -> None:
    with pytest.raises(AuditPolicyError, match="auditReportVersion 2"):
        evaluate_reports({"error": "registry unavailable"}, _report())


def test_missing_indirect_cause_fails_closed() -> None:
    malformed = {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "affected": {"severity": "critical", "via": ["missing-root"]},
        },
    }
    with pytest.raises(AuditPolicyError, match="missing cause"):
        evaluate_reports(malformed, _report())
