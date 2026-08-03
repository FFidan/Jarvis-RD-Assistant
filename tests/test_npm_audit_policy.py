"""Regression tests for the expiring frontend audit exceptions."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "frontend" / "osv-scanner.toml"
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_npm_audit.py"

_spec = importlib.util.spec_from_file_location("check_npm_audit", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_audit_policy = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, _audit_policy)
_spec.loader.exec_module(_audit_policy)

AuditPolicyError = _audit_policy.AuditPolicyError
BRACE_EXPANSION_ADVISORY = _audit_policy.BRACE_EXPANSION_ADVISORY
REACT_ROUTER_RSC_ADVISORY = _audit_policy.REACT_ROUTER_RSC_ADVISORY
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


def _report(
    *,
    include_brace: bool = False,
    include_router: bool = True,
    extra_advisory: str | None = None,
) -> dict[str, object]:
    vulnerabilities: dict[str, object] = {}
    if include_router:
        vulnerabilities.update(
            {
                "react-router": {
                    "severity": "high",
                    "via": [_advisory(REACT_ROUTER_RSC_ADVISORY, "react-router")],
                },
                "react-router-dom": {
                    "severity": "high",
                    "via": ["react-router"],
                },
            }
        )
    if include_brace:
        vulnerabilities.update(
            {
                "brace-expansion": {
                    "severity": "high",
                    "via": [_advisory(BRACE_EXPANSION_ADVISORY, "brace-expansion")],
                },
                "eslint": {
                    "severity": "high",
                    "via": ["brace-expansion"],
                },
            }
        )
    if extra_advisory is not None:
        vulnerabilities["unexpected-package"] = {
            "severity": "high",
            "via": [_advisory(extra_advisory, "unexpected-package")],
        }
    return {
        "auditReportVersion": 2,
        "vulnerabilities": vulnerabilities,
    }


def _stage_frontend(
    tmp_path: Path,
    *,
    rsc_dependency: bool = False,
    brace_version: str = "5.0.8",
) -> Path:
    frontend = tmp_path / "frontend"
    source = frontend / "src"
    source.mkdir(parents=True)
    dependencies = {"react-router-dom": "^7.18.1"}
    if rsc_dependency:
        dependencies["@react-router/dev"] = "^7.18.1"
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "dependencies": dependencies,
                "devDependencies": {},
                "overrides": {"brace-expansion@^5": "5.0.8"},
            }
        ),
        encoding="utf-8",
    )
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {},
                    "node_modules/brace-expansion": {"version": brace_version},
                },
            }
        ),
        encoding="utf-8",
    )
    (source / "app.tsx").write_text(
        "import { BrowserRouter } from 'react-router-dom';\n",
        encoding="utf-8",
    )
    return tmp_path


def test_expected_runtime_and_dev_advisories_pass(tmp_path: Path) -> None:
    """Only the checked RSC advisory may pass; the brace-expansion advisory is patched."""
    root = _stage_frontend(tmp_path)

    entries = evaluate_reports(
        _report(),
        _report(),
        repo_root=root,
        policy_path=POLICY_PATH,
        today=date(2026, 7, 28),
    )

    assert {entry.advisory_id for entry in entries} == {REACT_ROUTER_RSC_ADVISORY}


def test_unexpected_high_advisory_fails(tmp_path: Path) -> None:
    """A new high advisory cannot inherit an existing exception."""
    root = _stage_frontend(tmp_path)

    with pytest.raises(AuditPolicyError, match="unaccepted high-severity"):
        evaluate_reports(
            _report(extra_advisory="GHSA-1111-2222-3333"),
            _report(include_brace=False, extra_advisory="GHSA-1111-2222-3333"),
            repo_root=root,
            policy_path=POLICY_PATH,
            today=date(2026, 7, 28),
        )


def test_malformed_audit_report_fails(tmp_path: Path) -> None:
    """Registry or schema errors cannot be mistaken for a clean audit."""
    root = _stage_frontend(tmp_path)

    with pytest.raises(AuditPolicyError, match="auditReportVersion 2"):
        evaluate_reports(
            {"error": "registry unavailable"},
            _report(include_brace=False),
            repo_root=root,
            policy_path=POLICY_PATH,
            today=date(2026, 7, 28),
        )


def test_resolved_advisory_requires_exception_removal(tmp_path: Path) -> None:
    """A stale exception fails so the allowlist cannot grow indefinitely."""
    root = _stage_frontend(tmp_path)

    with pytest.raises(AuditPolicyError, match="remove resolved audit exceptions"):
        evaluate_reports(
            _report(include_router=False),
            _report(include_router=False),
            repo_root=root,
            policy_path=POLICY_PATH,
            today=date(2026, 7, 28),
        )


def test_expired_exception_fails(tmp_path: Path) -> None:
    """Expiry forces the two upstream conditions to be reviewed again."""
    root = _stage_frontend(tmp_path)

    with pytest.raises(AuditPolicyError, match="exception expired"):
        evaluate_reports(
            _report(),
            _report(),
            repo_root=root,
            policy_path=POLICY_PATH,
            today=date(2026, 10, 15),
        )


def test_rsc_dependency_invalidates_router_exception(tmp_path: Path) -> None:
    """Adding React Router framework/RSC packages reopens the advisory."""
    root = _stage_frontend(tmp_path, rsc_dependency=True)

    with pytest.raises(AuditPolicyError, match="direct packages"):
        evaluate_reports(
            _report(),
            _report(include_brace=False),
            repo_root=root,
            policy_path=POLICY_PATH,
            today=date(2026, 7, 28),
        )


def test_rsc_usage_outside_src_invalidates_router_exception(tmp_path: Path) -> None:
    """Server entrypoints outside src remain part of the exposure check."""
    root = _stage_frontend(tmp_path)
    (root / "frontend" / "server.ts").write_text(
        "export const router = RSCStaticRouter;\n",
        encoding="utf-8",
    )

    with pytest.raises(AuditPolicyError, match="RSC usage"):
        evaluate_reports(
            _report(),
            _report(include_brace=False),
            repo_root=root,
            policy_path=POLICY_PATH,
            today=date(2026, 7, 28),
        )


def test_unpatched_brace_v5_invalidates_dev_only_exception(tmp_path: Path) -> None:
    """The dev-only exception still requires the available v5 patch."""
    root = _stage_frontend(tmp_path, brace_version="5.0.7")

    with pytest.raises(AuditPolicyError, match="brace-expansion v5"):
        evaluate_reports(
            _report(),
            _report(include_brace=False),
            repo_root=root,
            policy_path=POLICY_PATH,
            today=date(2026, 7, 28),
        )
